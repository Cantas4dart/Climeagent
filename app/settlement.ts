import { DBManager, PaperTrade, Trade } from "./db";
import { PolyMarketAPI } from "./polymarket";
import { Bot } from "grammy";
import * as dotenv from "dotenv";
import * as fs from "fs";
import * as path from "path";
import { acquireProcessLock } from "./singleton";

dotenv.config();

const STARTUP_SETTLEMENT_DELAY_MS = 5_000;
const SETTLEMENT_CHECK_INTERVAL_MS = 5 * 60 * 1000;
const DAILY_REPORT_INTERVAL_MS = 10 * 60 * 1000;

function escapeHtml(value: string) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function buildAlignedMonoRow(left: string, right: string, leftWidth = 22) {
  return `${left.padEnd(leftWidth, " ")}${right}`;
}

export class SettlementMonitor {
  private db: DBManager;
  private bot: Bot;
  private lastDailyReport: string = "";
  private stateFile: string;
  private learningFeedbackFile: string;
  private settlementInFlight = false;

  constructor() {
    this.db = new DBManager();
    const token = process.env.TELEGRAM_BOT_TOKEN || "";
    this.bot = new Bot(token);
    this.stateFile = path.join(__dirname, "../data/settlement_state.json");
    this.learningFeedbackFile = path.join(__dirname, "../data/learning_feedback.jsonl");
    this.loadState();
  }

  private loadState() {
    try {
      if (fs.existsSync(this.stateFile)) {
        const data = JSON.parse(fs.readFileSync(this.stateFile, "utf-8"));
        this.lastDailyReport = data.lastDailyReport || "";
        console.log(`[SETTLE] Loaded state: lastDailyReport = ${this.lastDailyReport}`);
      }
    } catch (e: any) {
      console.warn(`[SETTLE] Could not load state: ${e.message}`);
    }
  }

  private saveState() {
    try {
      fs.writeFileSync(this.stateFile, JSON.stringify({
        lastDailyReport: this.lastDailyReport,
        updatedAt: new Date().toISOString()
      }, null, 2));
    } catch (e: any) {
      console.warn(`[SETTLE] Could not save state: ${e.message}`);
    }
  }

  private async fetchMarketSnapshot(
    poly: PolyMarketAPI,
    trade: { market_id: string; condition_id?: string | null }
  ) {
    const marketId = String(trade.market_id || "").trim();
    const conditionId = String(trade.condition_id || "").trim();

    if (marketId) {
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          const market = await poly.getMarketById(marketId);
          if (market && String(market.id || "") === marketId) {
            return market;
          }
          break;
        } catch (e: any) {
          const lastAttempt = attempt === 3;
          console.warn(
            `[SETTLE] Market lookup by market_id ${marketId} failed (attempt ${attempt}/3): ${e.message}`
          );
          if (lastAttempt) {
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, attempt * 1000));
        }
      }
    }

    if (conditionId) {
      for (let attempt = 1; attempt <= 2; attempt++) {
        try {
          const market = await poly.getMarketByConditionId(conditionId);
          if (market && String(market.conditionId || "").toLowerCase() === conditionId.toLowerCase()) {
            return market;
          }
          break;
        } catch (e: any) {
          const lastAttempt = attempt === 2;
          console.warn(
            `[SETTLE] Market lookup by condition_id ${conditionId} failed (attempt ${attempt}/2): ${e.message}`
          );
          if (lastAttempt) {
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, attempt * 1000));
        }
      }
    }

    return null;
  }

  async runLoop() {
    console.log("-----------------------------------------");
    console.log("Blocky Settlement Monitor Started (24/7)");
    console.log("-----------------------------------------");
    console.log(`[SETTLE] Startup check in ${Math.round(STARTUP_SETTLEMENT_DELAY_MS / 1000)}s.`);
    console.log(`[SETTLE] Recurring settlement checks every ${Math.round(SETTLEMENT_CHECK_INTERVAL_MS / 60000)} minutes.`);

    setTimeout(async () => {
      try {
        await this.runSettlementPass("startup");
      } catch (e: any) {
        console.error(`[SETTLE ERROR] Startup check failed: ${e.message}`);
      }
    }, STARTUP_SETTLEMENT_DELAY_MS);

    setInterval(async () => {
      try {
        await this.runSettlementPass("recurring");
      } catch (e: any) {
        console.error(`[SETTLE ERROR] Loop Error: ${e.message}`);
      }
    }, SETTLEMENT_CHECK_INTERVAL_MS);

    setInterval(async () => {
      try {
        await this.checkDailyReport();
      } catch (e: any) {
        console.error(`[DAILY ERROR] Report Error: ${e.message}`);
      }
    }, DAILY_REPORT_INTERVAL_MS);
  }

  private async runSettlementPass(mode: "startup" | "recurring") {
    if (this.settlementInFlight) {
      console.log(`[SETTLE] Skipping ${mode} settlement check because a previous pass is still running.`);
      return;
    }

    this.settlementInFlight = true;
    console.log(`[SETTLE] Running ${mode} settlement check...`);

    try {
      await this.repairStaleOpenTrades();
      await this.checkSettlements();
      console.log(`[SETTLE] ${mode.charAt(0).toUpperCase() + mode.slice(1)} settlement pass complete.`);
    } finally {
      this.settlementInFlight = false;
    }
  }

  private async checkSettlements() {
    console.log("[SETTLE] Settlement pass started.");
    await this.repairStaleOpenTrades();
    await this.checkPaperSettlements();

    const unsettled: Trade[] = this.db.getUnsettledTrades();
    if (unsettled.length === 0) {
      this.exportLearningFeedback();
      return;
    }

    console.log(`[SETTLE] Checking ${unsettled.length} unsettled trades...`);

    for (const trade of unsettled) {
      try {
        const poly = new PolyMarketAPI({ key: "", secret: "", passphrase: "" });
        const market = await this.fetchMarketSnapshot(poly, trade);

        if (!market || !market.closed) {
          continue;
        }

        const prices = JSON.parse(market.outcomePrices || "[]");
        if (prices.length < 2) {
          continue;
        }

        const winner = prices[0] === "1" ? "YES" : "NO";
        const win = trade.side === winner;
        const pnl = win ? (trade.size * (1 - trade.buy_price)) : -(trade.size * trade.buy_price);
        this.db.markSettled(trade.id, win ? 1 : 0, pnl);

        let claimMessage = "Manual claim available with /claim or /claim_all.";
        if (win) {
          const claimResult = await this.tryAutoClaim(trade);
          if (claimResult.claimed && claimResult.txHash) {
            claimMessage = `Auto-claimed: https://polygonscan.com/tx/${claimResult.txHash}`;
          } else if (claimResult.reason) {
            claimMessage = `Auto-claim skipped: ${claimResult.reason}`;
          }
        }

        const status = win ? "WIN" : "LOSS";
        const roi = win
          ? `+${((1 - trade.buy_price) / trade.buy_price * 100).toFixed(1)}%`
          : "-100%";
        await this.bot.api.sendMessage(
          trade.tg_id,
          this.buildRealSettlementMessage(trade, market.question || `Market ${trade.market_id}`, status as "WIN" | "LOSS", pnl, roi, claimMessage),
          { parse_mode: "HTML" }
        );
        console.log(`[SETTLE] Alerted user ${trade.tg_id} for market ${trade.market_id} (${status})`);
      } catch (e: any) {
        console.error(`[SETTLE ERROR] Market ${trade.market_id}: ${e.message}`);
      }
    }

    this.exportLearningFeedback();
    console.log("[SETTLE] Settlement pass complete.");
  }

  private async checkPaperSettlements() {
    const unsettledPaperTrades: PaperTrade[] = this.db.getUnsettledPaperTrades();
    if (unsettledPaperTrades.length === 0) {
      await this.sendPendingPaperSettlementAlerts();
      this.exportPaperLearningFeedback();
      return;
    }

    console.log(`[SETTLE] Checking ${unsettledPaperTrades.length} paper trade(s)...`);
    const poly = new PolyMarketAPI({ key: "", secret: "", passphrase: "" });

    for (const trade of unsettledPaperTrades) {
      try {
        const market = await this.fetchMarketSnapshot(poly, trade);

        if (!market || !market.closed) {
          continue;
        }

        const prices = JSON.parse(market.outcomePrices || "[]");
        if (!Array.isArray(prices) || prices.length < 2) {
          continue;
        }

        const winner = String(prices[0]) === "1" ? "YES" : "NO";
        const win = trade.side === winner;
        const pnl = win ? (trade.size * (1 - trade.entry_price)) : -(trade.size * trade.entry_price);
        this.db.markPaperTradeSettled(trade.id, win ? 1 : 0, pnl);
        await this.sendPaperSettlementAlert(
          {
            ...trade,
            outcome: win ? 1 : 0,
            pnl,
          },
          market.question || `Market ${trade.market_id}`
        );
      } catch (e: any) {
        console.warn(`[SETTLE] Paper settlement check failed for ${trade.id} / ${trade.market_id}: ${e.message}`);
      }
    }

    await this.sendPendingPaperSettlementAlerts();
    this.exportPaperLearningFeedback();
  }

  private async repairStaleOpenTrades() {
    const staleTrades = this.db.getStaleOpenTrades();
    if (staleTrades.length === 0) {
      return;
    }

    console.log(`[SETTLE] Repair scan: checking ${staleTrades.length} potentially stale open trade(s)...`);
    const poly = new PolyMarketAPI({ key: "", secret: "", passphrase: "" });

    for (const trade of staleTrades) {
      try {
        const market = await this.fetchMarketSnapshot(poly, trade);

        if (!market || !market.closed) {
          continue;
        }

        const prices = JSON.parse(market.outcomePrices || "[]");
        if (!Array.isArray(prices) || prices.length < 2) {
          continue;
        }

        const winner = String(prices[0]) === "1" ? "YES" : "NO";
        const win = trade.side === winner;
        const pnl = win ? (trade.size * (1 - trade.buy_price)) : -(trade.size * trade.buy_price);
        this.db.markSettled(trade.id, win ? 1 : 0, pnl);
        console.warn(
          `[SETTLE] Repaired stale open trade ${trade.id} / ${trade.market_id} as ${win ? "WIN" : "LOSS"}.`
        );
      } catch (e: any) {
        console.warn(`[SETTLE] Could not repair stale trade ${trade.id} / ${trade.market_id}: ${e.message}`);
      }
    }
  }

  private async tryAutoClaim(trade: Trade): Promise<{ claimed: boolean; txHash?: string; reason?: string }> {
    if (!trade.condition_id) {
      return { claimed: false, reason: "missing condition id" };
    }

    const user = this.db.getUser(trade.tg_id);
    if (!user) {
      return { claimed: false, reason: "user not found" };
    }
    if (!user.auto_claim) {
      return { claimed: false, reason: "auto-claim disabled for this user" };
    }

    try {
      const poly = new PolyMarketAPI({
        key: user.api_key || "",
        secret: user.api_secret || "",
        passphrase: user.api_passphrase || ""
      }, user.private_key || "", {
        funderAddress: user.funder_address || (process.env.POLY_FUNDER_ADDRESS || "").trim() || null,
        signatureType: Number.isInteger(user.signature_type)
          ? user.signature_type
          : ((process.env.POLY_SIGNATURE_TYPE || "").trim() ? Number.parseInt(process.env.POLY_SIGNATURE_TYPE || "", 10) : null),
      });

      const txHash = await poly.redeemWinnings(trade.condition_id);
      this.db.markClaimedByCondition(trade.tg_id, trade.condition_id, txHash);
      console.log(`[SETTLE] Auto-claimed condition ${trade.condition_id} for ${trade.tg_id}: ${txHash}`);
      return { claimed: true, txHash };
    } catch (e: any) {
      console.warn(`[SETTLE] Auto-claim failed for ${trade.tg_id} / ${trade.condition_id}: ${e.message}`);
      return { claimed: false, reason: e.message };
    }
  }

  private exportLearningFeedback() {
    const pending = this.db.getSettledTradesMissingFeedback();
    if (pending.length === 0) {
      return;
    }

    try {
      fs.mkdirSync(path.dirname(this.learningFeedbackFile), { recursive: true });
    } catch (e: any) {
      console.warn(`[SETTLE] Could not prepare learning feedback directory: ${e.message}`);
      return;
    }

    for (const trade of pending) {
      try {
        const payload = trade.learning_features ? JSON.parse(trade.learning_features) : null;
        if (!payload) {
          this.db.markFeedbackExported(trade.id);
          continue;
        }

        const resolvedYes = trade.side === "YES"
          ? Number(trade.outcome || 0)
          : Number(trade.outcome === null ? 0 : 1 - trade.outcome);

        const feedback = {
          feedback_id: `trade:${trade.id}`,
          trade_id: trade.id,
          market_id: trade.market_id,
          condition_id: trade.condition_id,
          side: trade.side,
          resolved_yes: resolvedYes,
          trade_won: Number(trade.outcome || 0),
          pnl: Number(trade.pnl || 0),
          entry_model_prob: trade.entry_model_prob,
          entry_market_prob: trade.entry_market_prob,
          entry_confidence: trade.entry_confidence,
          entry_spread: trade.entry_spread,
          entry_regime: trade.entry_regime,
          city: payload?.meta?.city ?? null,
          country: payload?.meta?.country ?? null,
          country_code: payload?.meta?.country_code ?? null,
          continent: payload?.meta?.continent ?? null,
          timezone: payload?.meta?.timezone ?? null,
          utc_offset_hours: payload?.meta?.utc_offset_hours ?? null,
          local_now: payload?.meta?.local_now ?? null,
          local_date: payload?.meta?.local_date ?? null,
          local_hour: payload?.meta?.local_hour ?? null,
          local_peak_stage: payload?.meta?.local_peak_stage ?? null,
          local_peak_stage_detail: payload?.meta?.local_peak_stage_detail ?? null,
          pattern_veto_applied: Boolean(payload?.decision?.pattern_veto_applied),
          yes_veto_applied: Boolean(payload?.decision?.yes_veto_applied),
          no_veto_applied: Boolean(payload?.decision?.no_veto_applied),
          learning_payload: payload,
          exported_at: new Date().toISOString(),
        };

        fs.appendFileSync(this.learningFeedbackFile, `${JSON.stringify(feedback)}\n`, "utf-8");
        this.db.markFeedbackExported(trade.id);
      } catch (e: any) {
        console.warn(`[SETTLE] Could not export learning feedback for trade ${trade.id}: ${e.message}`);
      }
    }
  }

  private exportPaperLearningFeedback() {
    const pending = this.db.getSettledPaperTradesMissingFeedback();
    if (pending.length === 0) {
      return;
    }

    try {
      fs.mkdirSync(path.dirname(this.learningFeedbackFile), { recursive: true });
    } catch (e: any) {
      console.warn(`[SETTLE] Could not prepare paper learning feedback directory: ${e.message}`);
      return;
    }

    for (const trade of pending) {
      try {
        const payload = trade.learning_features ? JSON.parse(trade.learning_features) : null;
        if (!payload) {
          this.db.markPaperFeedbackExported(trade.id);
          continue;
        }

        const resolvedYes = trade.side === "YES"
          ? Number(trade.outcome || 0)
          : Number(trade.outcome === null ? 0 : 1 - trade.outcome);

        const feedback = {
          feedback_id: `paper_trade:${trade.id}`,
          trade_id: trade.id,
          market_id: trade.market_id,
          condition_id: trade.condition_id,
          side: trade.side,
          resolved_yes: resolvedYes,
          trade_won: Number(trade.outcome || 0),
          pnl: Number(trade.pnl || 0),
          entry_model_prob: trade.entry_model_prob,
          entry_market_prob: trade.entry_market_prob,
          entry_confidence: trade.entry_confidence,
          entry_spread: trade.entry_spread,
          entry_regime: trade.entry_regime,
          city: payload?.meta?.city ?? null,
          country: payload?.meta?.country ?? null,
          country_code: payload?.meta?.country_code ?? null,
          continent: payload?.meta?.continent ?? null,
          timezone: payload?.meta?.timezone ?? null,
          utc_offset_hours: payload?.meta?.utc_offset_hours ?? null,
          local_now: payload?.meta?.local_now ?? null,
          local_date: payload?.meta?.local_date ?? null,
          local_hour: payload?.meta?.local_hour ?? null,
          local_peak_stage: payload?.meta?.local_peak_stage ?? null,
          local_peak_stage_detail: payload?.meta?.local_peak_stage_detail ?? null,
          pattern_veto_applied: Boolean(payload?.decision?.pattern_veto_applied),
          yes_veto_applied: Boolean(payload?.decision?.yes_veto_applied),
          no_veto_applied: Boolean(payload?.decision?.no_veto_applied),
          learning_payload: payload,
          source: "paper_trade",
          exported_at: new Date().toISOString(),
        };

        fs.appendFileSync(this.learningFeedbackFile, `${JSON.stringify(feedback)}\n`, "utf-8");
        this.db.markPaperFeedbackExported(trade.id);
      } catch (e: any) {
        console.warn(`[SETTLE] Could not export paper learning feedback for trade ${trade.id}: ${e.message}`);
      }
    }
  }

  private buildRealSettlementMessage(trade: Trade, marketQuestion: string, status: "WIN" | "LOSS", pnl: number, roi: string, claimMessage: string) {
    const marketTitle = escapeHtml(marketQuestion || `Market ${trade.market_id}`);
    const safeClaimMessage = escapeHtml(claimMessage);
    return [
      "<b>📄 Settlement</b>",
      "",
      "<b>🪙 Market</b>",
      `${marketTitle}`,
      "",
      "<b>🎯 Position</b>            <b>Result</b>",
      `<pre>${escapeHtml(`${trade.side} @ ${trade.buy_price.toFixed(4)}${" ".repeat(Math.max(1, 11 - `${trade.side} @ ${trade.buy_price.toFixed(4)}`.length))}${status === "WIN" ? "✅ WIN" : "❌ LOSS"}`)}</pre>`,
      `(${trade.size} share${trade.size === 1 ? "" : "s"})`,
      "",
      "<b>💰 PnL</b>                   <b>📉 ROI</b>",
      `<pre>${escapeHtml(`${pnl.toFixed(4)} pUSD${" ".repeat(Math.max(1, 11 - `${pnl.toFixed(4)} pUSD`.length))}${roi}`)}</pre>`,
      "",
      "<b>Claim Status</b>",
      `${safeClaimMessage}`,
      "",
      "<i>Use /stats for performance and /daily for the latest summary.</i>",
    ].join("\n");
  }

  private buildPaperSettlementMessage(trade: PaperTrade, marketQuestion: string) {
    const win = Number(trade.outcome || 0) === 1;
    const marketTitle = escapeHtml(marketQuestion || `Market ${trade.market_id}`);
    const paperPnl = Number(trade.pnl || 0);
    const roi = trade.entry_price > 0
      ? (win ? `+${(((1 - trade.entry_price) / trade.entry_price) * 100).toFixed(1)}%` : "-100%")
      : "N/A";

    return [
      "<b>📄 Paper Settlement</b>",
      "",
      "<b>🪙 Market</b>",
      `${marketTitle}`,
      "",
      "<b>🎯 Position</b>          <b>Result</b>",
      `${trade.side} @ ${trade.entry_price.toFixed(4)}${" ".repeat(Math.max(2, 28 - `${trade.side} @ ${trade.entry_price.toFixed(4)}`.length))}${win ? "🧪 ✅ WIN" : "🧪 ❌ LOSS"}`,
      `(${trade.size} share${trade.size === 1 ? "" : "s"})`,
      "",
      "<b>💰 PnL</b>                 <b>📉 ROI</b>",
      `${paperPnl.toFixed(4)} pUSD${" ".repeat(Math.max(2, 24 - `${paperPnl.toFixed(4)} pUSD`.length))}${roi}`,
      "",
      "<i>Simulation only — no real funds.</i>",
    ].join("\n");
  }

  private async sendPaperSettlementAlert(trade: PaperTrade, marketQuestion: string) {
    try {
      await this.bot.api.sendMessage(
        trade.tg_id,
        this.buildPaperSettlementMessage(trade, marketQuestion),
        { parse_mode: "HTML" }
      );
      this.db.markPaperAlertSent(trade.id);
      console.log(`[SETTLE] Sent paper settlement alert for ${trade.id} / ${trade.market_id}.`);
    } catch (e: any) {
      console.warn(`[SETTLE] Could not send paper settlement alert to ${trade.tg_id}: ${e.message}`);
    }
  }

  private async sendPendingPaperSettlementAlerts() {
    const pendingAlerts = this.db.getSettledPaperTradesPendingAlert();
    if (pendingAlerts.length === 0) {
      return;
    }

    console.log(`[SETTLE] Retrying ${pendingAlerts.length} pending paper settlement alert(s)...`);
    const poly = new PolyMarketAPI({ key: "", secret: "", passphrase: "" });

    for (const trade of pendingAlerts) {
      try {
        const market = await this.fetchMarketSnapshot(poly, trade);
        const marketQuestion = market?.question || `Market ${trade.market_id}`;
        await this.sendPaperSettlementAlert(trade, marketQuestion);
      } catch (e: any) {
        console.warn(`[SETTLE] Could not refresh paper market question for ${trade.id} / ${trade.market_id}: ${e.message}`);
        await this.sendPaperSettlementAlert(trade, `Market ${trade.market_id}`);
      }
    }
  }

  private async checkDailyReport() {
    const now = new Date();
    const hour = now.getUTCHours();
    const todayKey = now.toISOString().split("T")[0];

    if (hour !== 21 || this.lastDailyReport === todayKey) return;

    this.lastDailyReport = todayKey;
    this.saveState();
    console.log(`[DAILY] Sending daily performance reports for ${todayKey}...`);

    const activeUserIds = this.db.getAllActiveUserIds();

    for (const tgId of activeUserIds) {
      try {
        const daily = this.db.getDailyStats(tgId);
        const overall = this.db.getOverallStats(tgId);

        const dailyWinRate = daily.settled > 0
          ? ((daily.wins / daily.settled) * 100).toFixed(1)
          : "N/A";

        const overallROI = overall.settled > 0 && overall.total > 0
          ? `${(overall.pnl / (overall.total * 10) * 100).toFixed(1)}%`
          : "N/A";

        const report = `
*Evening Report - ${todayKey}*

*Today:*
Trades Placed: ${daily.total}
Settled: ${daily.settled}
Wins: ${daily.wins} (${dailyWinRate}%)
Day PnL: *${daily.pnl.toFixed(2)} pUSD*

*All Time:*
Total Trades: ${overall.total}
Settled: ${overall.settled}
Win Rate: ${overall.winRate}%
Cumulative PnL: *${overall.pnl.toFixed(2)} pUSD*
Estimated ROI: *${overallROI}*

_Automated daily report from Blocky_
        `;

        await this.bot.api.sendMessage(tgId, report, { parse_mode: "Markdown" });
        console.log(`[DAILY] Sent report to ${tgId}`);
      } catch (e: any) {
        console.error(`[DAILY ERROR] User ${tgId}: ${e.message}`);
      }
    }
  }
}

if (require.main === module) {
  const releaseLock = acquireProcessLock("settlement-monitor");
  if (!releaseLock) {
    process.exit(0);
  }
  const monitor = new SettlementMonitor();
  monitor.runLoop();
}
