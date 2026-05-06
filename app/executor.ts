import * as fs from "fs";
import * as path from "path";
import { Bot } from "grammy";
import { PolyMarketAPI } from "./polymarket";
import { DBManager, User } from "./db";
import { acquireProcessLock } from "./singleton";

function extractAllowance(balanceData: any): number {
  const directAllowance = parseFloat(balanceData?.allowance ?? "");
  if (!Number.isNaN(directAllowance)) {
    return directAllowance;
  }

  const standardEx = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";
  const legacyAllowance = parseFloat(balanceData?.allowances?.[standardEx] ?? "");
  return Number.isNaN(legacyAllowance) ? 0 : legacyAllowance;
}

function resolveUserPolymarketAccountConfig(user: any) {
  const rawFunder = (process.env.POLY_FUNDER_ADDRESS || "").trim();
  const rawSignatureType = (process.env.POLY_SIGNATURE_TYPE || "").trim();
  const fallbackSignatureType = rawSignatureType === "" ? null : Number.parseInt(rawSignatureType, 10);
  return {
    funderAddress: user?.funder_address || rawFunder || null,
    signatureType: Number.isInteger(user?.signature_type) ? user.signature_type : (Number.isInteger(fallbackSignatureType) ? fallbackSignatureType : null),
  };
}

function escapeHtml(value: string) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapePreBlock(value: string) {
  return escapeHtml(value).replace(/\n/g, "\n");
}

function buildAlignedMonoRow(left: string, right: string, leftWidth = 22) {
  return `${left.padEnd(leftWidth, " ")}${right}`;
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class TradeExecutor {
  private db: DBManager;
  private signalPath: string;
  private reservedCapitalByUser: Map<string, number>;
  private bot: Bot | null;

  constructor() {
    this.db = new DBManager();
    this.signalPath = path.join(__dirname, "../data/signals.json");
    this.reservedCapitalByUser = new Map();
    this.bot = process.env.TELEGRAM_BOT_TOKEN ? new Bot(process.env.TELEGRAM_BOT_TOKEN) : null;
  }

  async runLoop() {
    console.log("-----------------------------------------");
    console.log("Blocky Execution Loop Started (24/7 Mode)");
    console.log("-----------------------------------------");
    
    // Check every 2 minutes for new signals
    setInterval(async () => {
      try {
        await this.processSignals();
      } catch (e: any) {
        console.error(`[EXEC ERROR] Loop Error: ${e.message}`);
      }
    }, 120000); 
  }

  private async processSignals() {
    if (!fs.existsSync(this.signalPath)) {
      console.log("[EXEC] No signals file found. Waiting...");
      return;
    }
    
    const data = JSON.parse(fs.readFileSync(this.signalPath, "utf-8"));
    const signals = data.signals || [];
    const marketStates = data.market_states || [];

    await this.processOpenTrades(marketStates);
    await this.processPaperSignals(signals);

    if (signals.length === 0) {
      console.log("[EXEC] No active signals in file.");
      return;
    }

    const activeUsers: User[] = this.db.getActiveUsers();
    console.log(`[EXEC] Found ${activeUsers.length} active traders.`);
    this.reservedCapitalByUser.clear();

    for (const user of activeUsers) {
      const accountConfig = resolveUserPolymarketAccountConfig(user);
      const poly = new PolyMarketAPI({
        key: user.api_key || "",
        secret: user.api_secret || "",
        passphrase: user.api_passphrase || ""
      }, user.private_key || "", accountConfig);
      let openPositionCount = this.db.getUnsettledTradeCount(user.tg_id);

      for (const signal of signals) {
        if (openPositionCount >= user.max_open_positions) {
          console.log(
            `[EXEC] User ${user.tg_id} is at max open positions ` +
            `(${openPositionCount}/${user.max_open_positions}). Skipping further signals.`
          );
          break;
        }

        // 1. Check if user already traded this market
        if (this.db.hasTraded(user.tg_id, signal.market_id)) {
          continue;
        }

        console.log(
          `[EXEC] New Signal for ${user.tg_id}: ${signal.question} ` +
          `| ${signal.action} | mode=${signal.mode || "standard"} | conf=${signal.confidence_score ?? "n/a"}`
        );

        try {
          // 2. Size Calculation & Auto-Approval Check
          const balanceData: any = await poly.getBalance();
          const balance = parseFloat(balanceData.balance) / 1000000;
          const allowance = extractAllowance(balanceData) / 1000000;
          const alreadyReserved = this.reservedCapitalByUser.get(user.tg_id) || 0;
          const spendableBalance = Math.max(0, balance - alreadyReserved);
          
          console.log(
            `[EXEC] User ${user.tg_id} - Balance: ${balance.toFixed(2)}, ` +
            `Reserved: ${alreadyReserved.toFixed(2)}, Spendable: ${spendableBalance.toFixed(2)}, ` +
            `Allowance: ${allowance.toFixed(2)}`
          );

          // Auto-Approve if balance exists but allowance is missing
          if (balance > 0.1 && allowance < 1.0) {
            console.log(`[EXEC] Auto-approving pUSD allowance (Master Approval) for user ${user.tg_id}...`);
            await poly.approveCollateral();
            console.log(`[EXEC] Master Auto-approval transactions sent.`);
            // Continue with the loop, next run will pick up the new allowance
            continue;
          }

          // Confidence-weighted size = min(Balance * Risk%, MaxTradeAmount) * multiplier / entry price
          let targetUSD = spendableBalance * (user.risk_percent / 100);
          if (targetUSD > user.max_trade_amount) targetUSD = user.max_trade_amount;
          const sizeMultiplier = Math.max(0.25, Number(signal.size_multiplier || 1));
          targetUSD *= sizeMultiplier;

          const entryPrice = Number(signal.entry_price || signal.market_price);
          const size = Math.floor(targetUSD / entryPrice);
          const reservedCost = size * entryPrice;
          
          if (size < 1) {
            console.log(`[EXEC] Balance too low to place trade for ${user.tg_id}`);
            continue;
          }

          // 3. Get Token ID from Gamma API
          const marketData = await poly.getMarketById(signal.market_id);
          const clobTokenIds = JSON.parse(marketData.clobTokenIds);
          // 0 = Yes, 1 = No
          const tokenId = signal.action === "BUY_YES" ? clobTokenIds[0] : clobTokenIds[1];

          const side = signal.action.split("_")[1]; // YES or NO
          const reservation = this.db.reserveTrade({
            market_id: signal.market_id,
            condition_id: signal.condition_id,
            tg_id: user.tg_id,
            side,
            buy_price: entryPrice,
            size: size,
            entry_model_prob: signal.adjusted_model_prob ?? signal.avg_model_prob ?? null,
            entry_market_prob: signal.action === "BUY_YES" ? signal.market_price_yes : signal.market_price_no,
            entry_confidence: signal.confidence_score ?? null,
            entry_spread: signal.ensemble_spread ?? null,
            entry_regime: signal.regime ?? null,
            learning_features: signal.learning_features ? JSON.stringify(signal.learning_features) : null,
          });
          if (reservation.changes === 0) {
            console.log(`[EXEC] Trade already reserved or recorded for ${user.tg_id} on market ${signal.market_id}. Skipping.`);
            continue;
          }

          try {
            // 4. Place Order
            const orderResponse = await poly.placeLimitOrder(tokenId, "BUY", entryPrice, size);
            const orderId = orderResponse?.orderID != null ? String(orderResponse.orderID) : null;
            this.db.markTradeSubmitted(user.tg_id, signal.market_id, orderId);
            await this.sendTradeAlert(user.tg_id, signal, side, entryPrice, size, orderResponse);
          } catch (e: any) {
            this.db.releaseTradeReservation(user.tg_id, signal.market_id);
            throw e;
          }

          this.reservedCapitalByUser.set(user.tg_id, alreadyReserved + reservedCost);
          openPositionCount += 1;

          console.log(`[EXEC] Trade order submitted and saved for ${user.tg_id}`);

        } catch (e: any) {
          console.error(`[EXEC ERROR] User ${user.tg_id} failed trade: ${e.message}`);
        }
      }
    }
  }

  private async processPaperSignals(signals: any[]) {
    if (!Array.isArray(signals) || signals.length === 0) {
      return;
    }

    const paperUsers: User[] = this.db.getPaperTestingUsers();
    if (paperUsers.length === 0) {
      return;
    }

    for (const user of paperUsers) {
      for (const signal of signals) {
        if (this.db.hasPaperTrade(user.tg_id, signal.market_id)) {
          continue;
        }

        const side = signal.action.split("_")[1] as "YES" | "NO";
        const entryPrice = Number(signal.entry_price || signal.market_price);
        if (!Number.isFinite(entryPrice) || entryPrice <= 0) {
          continue;
        }

        const result = this.db.reservePaperTrade({
          market_id: signal.market_id,
          condition_id: signal.condition_id,
          tg_id: user.tg_id,
          side,
          entry_price: entryPrice,
          size: 1,
          entry_model_prob: signal.adjusted_model_prob ?? signal.avg_model_prob ?? null,
          entry_market_prob: signal.action === "BUY_YES" ? signal.market_price_yes : signal.market_price_no,
          entry_confidence: signal.confidence_score ?? null,
          entry_spread: signal.ensemble_spread ?? null,
          entry_regime: signal.regime ?? null,
          learning_features: signal.learning_features ? JSON.stringify(signal.learning_features) : null,
        });

        if (result.changes > 0) {
          await this.sendPaperTradeAlert(user.tg_id, signal, side, entryPrice);
          console.log(`[EXEC] Paper trade logged for ${user.tg_id} on ${signal.market_id}`);
        }
      }
    }
  }

  private async sendTradeAlert(
    tgId: string,
    signal: any,
    side: string,
    entryPrice: number,
    size: number,
    orderResponse: any
  ) {
    if (!this.bot) {
      return;
    }

    const lines = [
      "<b>📄 Signal</b>",
      "",
      "<b>🪙 Market</b>",
      escapeHtml(signal.question || signal.market_id),
      "",
      "<b>🎯 Position</b>          <b>⚙️ Mode</b>",
      buildAlignedMonoRow(`${side} @ ${entryPrice.toFixed(4)}`, signal.mode || "standard"),
      `(${size} share${size === 1 ? "" : "s"})`,
      "",
      "<b>📊 Confidence</b>",
      `<b>${Number(signal.confidence_score || 0).toFixed(2)}</b>`,
    ];

    if (orderResponse?.orderID) {
      lines.push("", "<b>🧾 Order ID</b>", escapeHtml(String(orderResponse.orderID)));
    }
    if (orderResponse?.status) {
      lines.push("<b>Status</b>", escapeHtml(String(orderResponse.status)));
    }

    lines.push("", "<i>Order accepted by Polymarket. It may still be waiting to fill.</i>");

    await this.sendTelegramAlert(tgId, lines.join("\n"), "trade");
  }

  private async sendPaperTradeAlert(
    tgId: string,
    signal: any,
    side: string,
    entryPrice: number,
  ) {
    if (!this.bot) {
      return;
    }

    const lines = [
      "<b>📄 Paper Signal</b>",
      "",
      "<b>🪙 Market</b>",
      escapeHtml(signal.question || signal.market_id),
      "",
      "<b>🎯 Position</b>          <b>⚙️ Mode</b>",
      buildAlignedMonoRow(`${side} @ ${entryPrice.toFixed(4)}`, signal.mode || "standard"),
      `(1 share)`,
      "",
      "<b>📊 Confidence</b>",
      `<b>${Number(signal.confidence_score || 0).toFixed(2)}</b>`,
      "",
      "<i>No real order was placed. This signal will be scored when the market settles.</i>",
    ];

    await this.sendTelegramAlert(tgId, lines.join("\n"), "paper trade");
  }

  private async processOpenTrades(marketStates: any[]) {
    if (!Array.isArray(marketStates) || marketStates.length === 0) {
      return;
    }

    const stateByMarket = new Map<string, any>();
    for (const state of marketStates) {
      if (state?.market_id) {
        stateByMarket.set(String(state.market_id), state);
      }
    }

    const activeTrades = this.db.getActiveTradesForMonitoring();
    if (activeTrades.length === 0) {
      return;
    }

    const openOrderIdsByUser = new Map<string, Set<string>>();

    for (const trade of activeTrades) {
      const state = stateByMarket.get(String(trade.market_id));
      if (!state) {
        continue;
      }

      const user = this.db.getUser(trade.tg_id);
      if (!user || !user.trading_active) {
        continue;
      }

      const assessment = this.assessConflict(trade, state);
      if (assessment.exitFraction <= 0) {
        continue;
      }

      const remainingSize = Math.max(0, Math.floor(Number(trade.remaining_size || trade.size || 0)));
      if (remainingSize < 1) {
        continue;
      }

      const sharesToSell = Math.max(1, Math.min(remainingSize, Math.floor(remainingSize * assessment.exitFraction)));
      const accountConfig = resolveUserPolymarketAccountConfig(user);
      const poly = new PolyMarketAPI({
        key: user.api_key || "",
        secret: user.api_secret || "",
        passphrase: user.api_passphrase || ""
      }, user.private_key || "", accountConfig);

      if (!openOrderIdsByUser.has(trade.tg_id)) {
        try {
          const openOrders = await poly.getOpenOrders();
          const openIds = new Set<string>();
          for (const order of openOrders || []) {
            const orderView = order as any;
            const rawId = orderView?.id ?? orderView?.orderID ?? orderView?.orderId;
            if (rawId != null) {
              openIds.add(String(rawId));
            }
          }
          openOrderIdsByUser.set(trade.tg_id, openIds);
        } catch (e: any) {
          console.warn(`[EXEC] Could not refresh open orders for conflict monitor ${trade.tg_id}: ${e.message}`);
          openOrderIdsByUser.set(trade.tg_id, new Set<string>());
        }
      }

      if (trade.order_id && openOrderIdsByUser.get(trade.tg_id)?.has(String(trade.order_id))) {
        console.log(
          `[EXEC] Conflict monitor is skipping ${trade.market_id} for ${trade.tg_id} ` +
          `because entry order ${trade.order_id} is still open on Polymarket.`
        );
        continue;
      }

      try {
        const marketData = await poly.getMarketById(trade.market_id);
        const clobTokenIds = JSON.parse(marketData.clobTokenIds);
        const tokenId = trade.side === "YES" ? clobTokenIds[0] : clobTokenIds[1];
        const orderResponse = await poly.placeMarketOrder(tokenId, "SELL", sharesToSell);

        const newRemainingSize = Math.max(0, remainingSize - sharesToSell);
        const exitPrice = trade.side === "YES"
          ? Number(state.market_price_yes ?? trade.buy_price)
          : Number(state.market_price_no ?? trade.buy_price);
        const fullyClosed = newRemainingSize < 1;
        this.db.recordTradeExit(trade.id, newRemainingSize, exitPrice, assessment.reason, fullyClosed);
        await this.sendExitAlert(trade.tg_id, trade, state, sharesToSell, newRemainingSize, assessment, orderResponse);
        console.log(
          `[EXEC] Auto-exit submitted for ${trade.tg_id} on market ${trade.market_id}: ` +
          `${sharesToSell}/${remainingSize} shares, remaining=${newRemainingSize}`
        );
      } catch (e: any) {
        console.error(`[EXEC ERROR] Auto-exit failed for ${trade.tg_id} / ${trade.market_id}: ${e.message}`);
      }
    }
  }

  private assessConflict(trade: any, state: any) {
    const entryModelProb = Number(trade.entry_model_prob ?? trade.buy_price);
    const entryMarketProb = Number(trade.entry_market_prob ?? trade.buy_price);
    const entryConfidence = Number(trade.entry_confidence ?? 0.80);
    const entrySpread = Number(trade.entry_spread ?? 0.10);
    const currentModelProb = trade.side === "YES"
      ? Number(state.adjusted_model_prob ?? 0.5)
      : 1 - Number(state.adjusted_model_prob ?? 0.5);
    const currentMarketProb = trade.side === "YES"
      ? Number(state.market_price_yes ?? trade.buy_price)
      : Number(state.market_price_no ?? trade.buy_price);
    const currentConfidence = Number(state.confidence_score ?? entryConfidence);
    const currentSpread = Number(state.ensemble_spread ?? entrySpread);

    const entryGap = Math.max(0, entryModelProb - entryMarketProb);
    const currentGap = currentModelProb - currentMarketProb;
    const gapDeterioration = Math.max(0, entryGap - currentGap);
    const adverseMomentum = Math.max(0, entryMarketProb - currentMarketProb);
    const spreadWidening = Math.max(0, currentSpread - entrySpread);
    const confidenceDrop = Math.max(0, entryConfidence - currentConfidence);
    const thesisFlip = state.action && state.action !== `BUY_${trade.side}` ? 0.35 : 0.0;

    let regimeShift = 0.0;
    if (trade.entry_regime === "post_peak" && state.regime === "pre_peak") {
      regimeShift = 0.25;
    } else if (trade.entry_regime === "near_peak" && state.regime === "pre_peak") {
      regimeShift = 0.18;
    }

    const modelConflict = currentModelProb < currentMarketProb ? Math.min((currentMarketProb - currentModelProb) * 2.5, 0.35) : 0.0;
    const bustStress = Math.min(Number(state.bust_risk ?? 0) * 1.4, 0.15);

    const conflictScore = Math.min(
      1.0,
      (gapDeterioration * 1.8)
      + (adverseMomentum * 1.3)
      + (spreadWidening * 1.0)
      + (confidenceDrop * 0.9)
      + regimeShift
      + thesisFlip
      + modelConflict
      + bustStress
    );

    let exitFraction = 0.0;
    if (conflictScore >= 0.85 || currentModelProb <= 0.50) {
      exitFraction = 1.0;
    } else if (conflictScore >= 0.70) {
      exitFraction = 0.75;
    } else if (conflictScore >= 0.55) {
      exitFraction = 0.50;
    } else if (conflictScore >= 0.35) {
      exitFraction = 0.25;
    }

    const reasons = [];
    if (gapDeterioration > 0.03) reasons.push("edge deterioration");
    if (adverseMomentum > 0.04) reasons.push("adverse price momentum");
    if (spreadWidening > 0.04) reasons.push("spread widening");
    if (confidenceDrop > 0.08) reasons.push("confidence weakening");
    if (regimeShift > 0) reasons.push("regime shift");
    if (thesisFlip > 0) reasons.push("action flip");
    if (modelConflict > 0.10) reasons.push("market/model divergence");
    if (bustStress > 0.08) reasons.push("bust risk increase");

    return {
      conflictScore: Number(conflictScore.toFixed(4)),
      exitFraction,
      reason: reasons.length > 0 ? reasons.join(", ") : "conflict monitor triggered",
    };
  }

  private async sendExitAlert(
    tgId: string,
    trade: any,
    state: any,
    sharesSold: number,
    remainingSize: number,
    assessment: any,
    orderResponse: any
  ) {
    if (!this.bot) {
      return;
    }

    const lines = [
      "<b>📉 Conflict Exit</b>",
      "",
      "<b>🪙 Market</b>",
      escapeHtml(state.question || trade.market_id),
      "",
      "<b>🎯 Side Reduced</b>          <b>⚙️ Reason</b>",
      `${trade.side}${" ".repeat(Math.max(1, 16 - String(trade.side).length))}${assessment.reason}`,
      `Sold ${sharesSold} | Left ${remainingSize}`,
      "",
      "<b>📊 Conflict Score</b>",
      `<b>${Number(assessment.conflictScore).toFixed(2)}</b>`,
    ];

    if (orderResponse?.orderID) {
      lines.push(`<b>Order ID</b>  ${escapeHtml(String(orderResponse.orderID))}`);
    }
    if (orderResponse?.status) {
      lines.push(`<b>Status</b>  ${escapeHtml(String(orderResponse.status))}`);
    }

    await this.sendTelegramAlert(tgId, lines.join("\n"), "exit");
  }

  private async sendTelegramAlert(tgId: string, message: string, kind: string) {
    if (!this.bot) {
      return;
    }

    const maxAttempts = 3;
    let lastError: any = null;

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        await this.bot.api.sendMessage(tgId, message, { parse_mode: "HTML" });
        return;
      } catch (e: any) {
        lastError = e;
        const reason = String(e?.message || e);
        if (attempt < maxAttempts) {
          console.warn(
            `[EXEC] Telegram ${kind} alert failed for ${tgId} on attempt ${attempt}/${maxAttempts}: ${reason}. Retrying...`
          );
          await sleep(1000 * attempt);
          continue;
        }
        console.warn(`[EXEC] Could not send ${kind} alert to ${tgId}: ${reason}`);
      }
    }

    if (lastError) {
      console.warn(`[EXEC] Telegram ${kind} alert gave up for ${tgId}.`);
    }
  }
}

if (require.main === module) {
  const releaseLock = acquireProcessLock("trade-executor");
  if (!releaseLock) {
    process.exit(0);
  }
  const executor = new TradeExecutor();
  executor.runLoop();
}
