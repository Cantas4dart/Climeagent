import { Bot, Context, InlineKeyboard, session, SessionFlavor } from "grammy";
import * as dotenv from "dotenv";
import { DBManager } from "./db";
import { CryptoManager } from "./crypto";
import { PolyMarketAPI } from "./polymarket";
import { privateKeyToAccount } from "viem/accounts";
import { acquireProcessLock } from "./singleton";

dotenv.config();

function getDefaultPolymarketAccountConfig() {
  const rawFunder = (process.env.POLY_FUNDER_ADDRESS || "").trim();
  const rawSignatureType = (process.env.POLY_SIGNATURE_TYPE || "").trim();
  const signatureType = rawSignatureType === "" ? null : Number.parseInt(rawSignatureType, 10);
  const funderAddress = rawFunder && !rawFunder.includes("your_polymarket") ? rawFunder : null;

  return {
    funderAddress,
    signatureType: Number.isInteger(signatureType) ? signatureType : null,
  };
}

function resolveUserPolymarketAccountConfig(user: any) {
  const defaults = getDefaultPolymarketAccountConfig();
  return {
    funderAddress: user?.funder_address || defaults.funderAddress,
    signatureType: Number.isInteger(user?.signature_type) ? user.signature_type : defaults.signatureType,
  };
}

function extractAllowance(balanceData: any): number {
  const directAllowance = parseFloat(balanceData?.allowance ?? "");
  if (!Number.isNaN(directAllowance)) {
    return directAllowance;
  }

  const standardEx = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";
  const legacyAllowance = parseFloat(balanceData?.allowances?.[standardEx] ?? "");
  return Number.isNaN(legacyAllowance) ? 0 : legacyAllowance;
}

interface SessionData {
  step: string;
  pending_private_key?: string;
  pending_funder_address?: string;
}

type MyContext = Context & SessionFlavor<SessionData>;

const botToken = process.env.TELEGRAM_BOT_TOKEN || "";
if (!botToken) {
  console.error("CRITICAL ERROR: TELEGRAM_BOT_TOKEN is missing in .env");
}

const bot = new Bot<MyContext>(botToken);
const db = new DBManager();
const crypto = new CryptoManager();
const DASHBOARD_POSITIONS_LIMIT = 6;
const DASHBOARD_ORDERS_LIMIT = 6;
const CLAIMABLE_BUTTON_LIMIT = 5;

function hasImportedWallet(user?: any): boolean {
  return !!(
    user &&
    typeof user.private_key === "string" && user.private_key.trim() &&
    typeof user.api_key === "string" && user.api_key.trim() &&
    typeof user.api_secret === "string" && user.api_secret.trim() &&
    typeof user.api_passphrase === "string" && user.api_passphrase.trim()
  );
}

function truncateMiddle(value: string, start = 8, end = 6): string {
  if (!value || value.length <= start + end + 3) return value;
  return `${value.slice(0, start)}...${value.slice(-end)}`;
}

function formatPositionSummary(position: any): string {
  const asset = position.displayLabel || position.title || position.asset || "Unknown asset";
  const size = position.size ?? position.balance ?? "?";
  const avgPrice = position.avgPrice ?? position.averagePrice ?? "?";
  return `• ${asset}: ${size} @ ${avgPrice}`;
}

function formatOrderSummary(order: any): string {
  const label = order.outcome || order.asset_id || order.market || "Order";
  const size = order.original_size || order.size || "?";
  const price = order.price ?? "?";
  const status = order.status || "open";
  return `\u{1F4CB} ${label}: ${order.side} ${size} @ ${price} (${status})`;
}

function formatRealTradeHistory(trade: any): string {
  const result = trade.settled ? (trade.outcome === 1 ? "WIN" : "LOSS") : "OPEN";
  const pnl = trade.settled ? ` | PnL ${Number(trade.pnl || 0).toFixed(2)} pUSD` : "";
  return `\u{1F4DC} #${trade.id} ${trade.side} ${trade.market_id} | ${result}${pnl}`;
}

function formatPaperTradeHistory(trade: any): string {
  const result = trade.settled ? (trade.outcome === 1 ? "WIN" : "LOSS") : "OPEN";
  const pnl = trade.settled ? ` | PnL ${Number(trade.pnl || 0).toFixed(4)} pUSD` : "";
  return `\u{1F4DC} #${trade.id} ${trade.side} ${trade.market_id} | ${result}${pnl}`;
}

function buildPositionsKeyboard(autoClaim: boolean, hasClaimables: boolean) {
  const keyboard = new InlineKeyboard()
    .text("\u{1F3E0} Home", "positions:main")
    .text("\u{1F504} Refresh", "positions:refresh")
    .row()
    .text("\u{1F4B0} Claimable", "positions:claimable")
    .text("\u{1F6E0}\uFE0F Setup", "positions:setup")
    .row()
    .text("\u{1F4E6} Orders", "positions:orders")
    .text("\u{1F4D1} History", "positions:history")
    .row()
    .text("\u{1F4B5} Balance", "positions:balance")
    .text("\u{1F4CA} Status", "positions:status")
    .row()
    .text("\u{1F4C8} Stats", "positions:stats")
    .text("\u{1F4C5} Daily", "positions:daily")
    .row()
    .text("\u2699\ufe0f Controls", "positions:controls")
    .text("\u2753 Help", "positions:help");

  keyboard.row();

  keyboard
    .text("\u{1F4B0} Claim All", hasClaimables ? "positions:claim_all" : "positions:claimable")
    .text(autoClaim ? "\u{1F6D1} Auto-Claim Off" : "\u2705 Auto-Claim On", autoClaim ? "positions:auto_claim_off" : "positions:auto_claim_on");

  return keyboard;
}

function buildOnboardingKeyboard() {
  return new InlineKeyboard()
    .text("\u{1F4BC} Real Trade", "positions:real_trade")
    .text("\u{1F9EA} Paper Trade", "positions:paper_trade")
    .row()
    .text("\u{1F6E0}\uFE0F Setup", "positions:setup")
    .text("\u2753 Help", "positions:help")
    .row()
    .text("\u{1F3E0} Home", "positions:welcome");
}


function buildSetupMessage(user?: any) {
  if (!user || !hasImportedWallet(user)) {
    return wrapCodeBlock([
      "Setup Center",
      "",
      user
        ? "No wallet is currently attached. Choose Real Trade to import one, or use Paper Trade without a live wallet."
        : "Import your wallet first to unlock approvals, wallet checks, funding, and risk controls.",
      "",
      "Available After Import",
      formatKeyValue("Approve", "trading allowance"),
      formatKeyValue("Check", "signer, funder, proxy"),
      formatKeyValue("Move", "pUSD into the trading wallet"),
      formatKeyValue("Tune", "risk, max size, max open"),
      "",
      "Testing",
      formatKeyValue("Paper Signal", user?.paper_testing_active ? "ON" : "OFF"),
    ]);
  }

  const accountConfig = resolveUserPolymarketAccountConfig(user);
  return wrapCodeBlock([
    "Setup Center",
    "",
    "Wallet",
    formatKeyValue("Funder", accountConfig.funderAddress || "wallet address"),
    formatKeyValue("Signature Type", accountConfig.signatureType ?? "default(EOA)"),
    "",
    "Ready Actions",
    formatKeyValue("Import", "replace wallet credentials"),
    formatKeyValue("Approve", "refresh trading allowance"),
    formatKeyValue("Wallet Check", "verify signer and proxy"),
    formatKeyValue("Fund Wallet", "move pUSD into the trading wallet"),
    formatKeyValue("Risk Settings", "update trade limits and exposure"),
    formatKeyValue("Remove Wallet", "wipe live credentials"),
    "",
    "Testing",
    formatKeyValue("Paper Signal", user.paper_testing_active ? "ON" : "OFF"),
  ]);
}

function buildControlsMessage(user: any) {
  return wrapCodeBlock([
    "Controls Center",
    "",
    "Trading",
    formatKeyValue("Status", user.trading_active ? "Active" : "Stopped"),
    formatKeyValue("Auto-Claim", user.auto_claim ? "ON" : "OFF"),
    formatKeyValue("Paper Testing", user.paper_testing_active ? "ON" : "OFF"),
    "",
    "Exposure",
    formatKeyValue("Risk", `${user.risk_percent}%`),
    formatKeyValue("Max Trade", `$${user.max_trade_amount}`),
    formatKeyValue("Max Open", user.max_open_positions),
    "",
    "Use the buttons below to start or stop trading, toggle auto-claim, or open risk settings.",
  ]);
}

function buildRiskSettingsMessage(user: any) {
  return wrapCodeBlock([
    "Risk Settings",
    "",
    formatKeyValue("Risk Per Trade", `${user.risk_percent}%`),
    formatKeyValue("Max Trade Amount", `$${user.max_trade_amount}`),
    formatKeyValue("Max Open Positions", user.max_open_positions),
    formatKeyValue("Paper Testing", user.paper_testing_active ? "ON" : "OFF"),
    "",
    "Choose a setting below and then send the new value in chat.",
  ]);
}

async function buildWalletCheckMessage(user: any) {
  const accountConfig = resolveUserPolymarketAccountConfig(user);
  const poly = new PolyMarketAPI({
    key: user.api_key,
    secret: user.api_secret,
    passphrase: user.api_passphrase
  }, user.private_key, accountConfig);

  const signerAddress = poly.getSignerAddress();
  const funderAddress = poly.getConfiguredFunderAddress();
  const profile = await poly.getPublicProfileByWallet(funderAddress);
  const proxyWallet = profile?.proxyWallet || null;

  return wrapCodeBlock([
    "Wallet Check",
    "",
    formatKeyValue("Signer", signerAddress),
    formatKeyValue("Configured Funder", funderAddress),
    formatKeyValue("Profile Proxy", proxyWallet || "not found"),
    formatKeyValue("Signature Type", accountConfig.signatureType ?? "default(EOA)"),
    "",
    proxyWallet && proxyWallet.toLowerCase() === funderAddress.toLowerCase()
      ? "Funder matches Polymarket profile proxy wallet."
      : "Funder does not match the Polymarket profile proxy wallet, or no profile was found.",
  ]);
}

function buildWelcomeDashboard() {
  return wrapCodeBlock([
    "Blocky Welcome",
    "",
    "Pick the mode you want to use first.",
    "",
    "Real Trade",
    "Import your wallet, approve spending, fund the trading wallet, and manage live positions.",
    "",
    "Paper Trade",
    "Turn on paper testing and let the bot score signals without placing live orders.",
  ]);
}

function escapeHtml(text: string): string {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function wrapCodeBlock(lines: string[]): string {
  return lines.join("\n");
}

function formatKeyValue(label: string, value: any, width = 17): string {
  return `${label.padEnd(width, " ")} ${value}`;
}

function buildDetailKeyboard(autoClaim: boolean, hasClaimables: boolean, page: string) {
  const keyboard = new InlineKeyboard()
    .text("🏠 Home", "positions:main")
    .text("⬅️ Dashboard", "positions:refresh")
    .text("🔄 Refresh", `positions:${page}`)
    .row()
    .text("📋 Claimable", "positions:claimable")
    .text("🛠 Setup", "positions:setup")
    .row()
    .text("💰 Balance", "positions:balance")
    .text("📊 Status", "positions:status")
    .row()
    .text("📈 Stats", "positions:stats")
    .text("🗓 Daily", "positions:daily")
    .row()
    .text("⚙️ Controls", "positions:controls")
    .text("❓ Help", "positions:help")
    .row();

  keyboard.text("💸 Claim All", hasClaimables ? "positions:claim_all" : "positions:claimable");
  keyboard.text(
    autoClaim ? "🛑 Auto-Claim Off" : "✅ Auto-Claim On",
    autoClaim ? "positions:auto_claim_off" : "positions:auto_claim_on"
  );

  return keyboard;
}

function buildSetupKeyboard(hasUser: boolean, hasWallet: boolean) {
  const keyboard = new InlineKeyboard()
    .text("🏠 Home", "positions:main")
    .text("🔄 Refresh", "positions:setup")
    .row()
    .text("🔐 Import Wallet", "positions:import_start")
    .text("✅ Approve", hasWallet ? "positions:approve" : "positions:import_start")
    .row()
    .text("🔎 Wallet Check", hasWallet ? "positions:wallet_check" : "positions:import_start")
    .text("💸 Fund Wallet", hasWallet ? "positions:fund_prompt" : "positions:import_start")
    .row()
    .text("🎯 Risk Settings", hasUser ? "positions:risk_settings" : "positions:help")
    .text("⚙️ Controls", hasUser ? "positions:controls" : "positions:help")
    .row()
    .text(hasWallet ? "🗑 Remove Wallet" : "🔐 Import Wallet", hasWallet ? "positions:remove_wallet" : "positions:import_start")
    .text("❓ Help", "positions:help");

  return keyboard;
}

function buildRiskKeyboard() {
  return new InlineKeyboard()
    .text("⬅️ Setup", "positions:setup")
    .text("🔄 Refresh", "positions:risk_settings")
    .row()
    .text("🎯 Set Risk %", "positions:risk_prompt")
    .text("💵 Set Max $", "positions:max_prompt")
    .row()
    .text("📦 Set Max Open", "positions:max_open_prompt")
    .text("⚙️ Controls", "positions:controls");
}

function buildControlsKeyboard(user: any) {
  return new InlineKeyboard()
    .text("🏠 Home", "positions:main")
    .text("🔄 Refresh", "positions:controls")
    .row()
    .text(user?.trading_active ? "⏸ Stop Trading" : "▶️ Start Trading", user?.trading_active ? "positions:stop_trading" : "positions:start_trading")
    .text(user?.auto_claim ? "🛑 Auto-Claim Off" : "✅ Auto-Claim On", user?.auto_claim ? "positions:auto_claim_off" : "positions:auto_claim_on")
    .row()
    .text(user?.paper_testing_active ? "🧪 Paper Testing Off" : "🧪 Paper Testing On", user?.paper_testing_active ? "positions:paper_testing_off" : "positions:paper_testing_on")
    .text("🎯 Risk Settings", "positions:risk_settings")
    .row()
    .text("🛠 Setup", "positions:setup")
    .text("📊 Status", "positions:status")
    .row()
    .text("❓ Help", "positions:help");
}

function buildClaimableKeyboard(claimableTrades: any[], autoClaim: boolean) {
  const keyboard = new InlineKeyboard();
  claimableTrades.slice(0, CLAIMABLE_BUTTON_LIMIT).forEach((trade, index) => {
    const marketId = String(trade.market_id || "");
    const tradeId = String(trade.id || "");
    const label = `${index + 1}. ${trade.side === "NO" ? "🔴" : "🟢"} ${truncateMiddle(marketId, 8, 4)}`;
    keyboard.text(label, `positions:claim:${tradeId}`).row();
  });

  if (claimableTrades.length > 0) {
    keyboard.text("💸 Claim All", "positions:claim_all").row();
  }

  keyboard
    .text(autoClaim ? "🛑 Auto-Claim Off" : "✅ Auto-Claim On", autoClaim ? "positions:auto_claim_off" : "positions:auto_claim_on")
    .row()
    .text("⬅️ Back", "positions:refresh")
    .text("🔄 Refresh", "positions:claimable");

  return keyboard;
}

async function buildBalanceMessage(user: any) {
  const accountConfig = resolveUserPolymarketAccountConfig(user);
  const poly = new PolyMarketAPI({
    key: user.api_key,
    secret: user.api_secret,
    passphrase: user.api_passphrase
  }, user.private_key, accountConfig);
  const balanceData: any = await poly.getBalance();

  if (!balanceData || balanceData.balance === undefined) {
    throw new Error("Invalid balance data received from Polymarket.");
  }

  const balanceNum = parseFloat(balanceData.balance);
  const allowanceNum = extractAllowance(balanceData);
  const signerAddress = poly.getSignerAddress();
  const funderAddress = poly.getConfiguredFunderAddress();
  const signerWalletPusd = await poly.getWalletPusdBalance(signerAddress);
  const funderWalletPusd = signerAddress.toLowerCase() === funderAddress.toLowerCase()
    ? signerWalletPusd
    : await poly.getWalletPusdBalance(funderAddress);

  const formattedBalance = isNaN(balanceNum) ? "0.00" : (balanceNum / 1000000).toFixed(2);
  const formattedAllowance = isNaN(allowanceNum) ? "0.00" : (allowanceNum / 1000000).toFixed(2);
  const signerWalletPusdText = (Number(signerWalletPusd) / 1_000_000).toFixed(2);
  const funderWalletPusdText = (Number(funderWalletPusd) / 1_000_000).toFixed(2);

  return wrapCodeBlock([
    "pUSD Status",
    "",
    formatKeyValue("Trading Balance", `${formattedBalance} pUSD`),
    formatKeyValue("Trading Allowance", `${formattedAllowance} pUSD`),
    formatKeyValue("Signature Type", accountConfig.signatureType ?? "default(EOA)"),
    formatKeyValue("Signer", signerAddress),
    formatKeyValue("Signer Wallet pUSD", `${signerWalletPusdText} pUSD`),
    formatKeyValue("Funder", funderAddress),
    formatKeyValue("Funder Wallet pUSD", `${funderWalletPusdText} pUSD`),
    "",
    "Note: If balance is wrong, ensure your wallet holds pUSD on Polygon.",
  ]);
}

function buildStatusMessage(user: any) {
  const accountConfig = resolveUserPolymarketAccountConfig(user);
  return wrapCodeBlock([
    "Bot Status",
    "",
    "Controls",
    formatKeyValue("Trading", user.trading_active ? "Active" : "Stopped"),
    formatKeyValue("Auto-Claim", user.auto_claim ? "ON" : "OFF"),
    "",
    "Risk Profile",
    formatKeyValue("Risk", `${user.risk_percent}%`),
    formatKeyValue("Max Trade", `$${user.max_trade_amount}`),
    formatKeyValue("Max Open Positions", user.max_open_positions),
    "",
    "Account",
    formatKeyValue("Signature Type", accountConfig.signatureType ?? "default(EOA)"),
    formatKeyValue("Funder", accountConfig.funderAddress || "wallet address"),
  ]);
}

function buildStatsMessage(overall: any, paperStats?: any) {
  const lines = [
    "Overall Performance",
    "",
    formatKeyValue("Total Trades", overall.total),
    formatKeyValue("Settled", overall.settled),
    formatKeyValue("Win Rate", `${overall.winRate}%`),
    formatKeyValue("Total PnL", `${overall.pnl.toFixed(2)} pUSD`),
  ];

  if (paperStats) {
    lines.push("", ...buildPaperStatsLines(paperStats));
  }

  return wrapCodeBlock(lines);
}

function buildPaperStatsLines(paperStats: any) {
  const hitRateLabel = paperStats.settled > 0 ? `${paperStats.winRate}% hit rate` : "No settled paper tests yet";
  return [
    "Paper Test Lab",
    formatKeyValue("Total Signals Tracked", paperStats.total),
    formatKeyValue("Open Simulations", paperStats.open),
    formatKeyValue("Settled Simulations", paperStats.settled),
    formatKeyValue("Scoreline", `${paperStats.wins} win${paperStats.wins === 1 ? "" : "s"} / ${paperStats.losses} loss${paperStats.losses === 1 ? "" : "es"}`),
    formatKeyValue("Paper Edge", hitRateLabel),
    formatKeyValue("Paper PnL", `${paperStats.pnl.toFixed(3)} pUSD`),
  ];
}

function buildDailyMessage(daily: any, overall: any) {
  const today = new Date().toISOString().split("T")[0];
  const dailyWinRate = daily.settled > 0 ? ((daily.wins / daily.settled) * 100).toFixed(1) : "N/A";

  return wrapCodeBlock([
    `Daily Report - ${today}`,
    "",
    "Today",
    formatKeyValue("Trades", daily.total),
    formatKeyValue("Settled", daily.settled),
    formatKeyValue("Wins", `${daily.wins} (${dailyWinRate}%)`),
    formatKeyValue("PnL", `${daily.pnl.toFixed(2)} pUSD`),
    "",
    "All Time",
    formatKeyValue("Total Trades", overall.total),
    formatKeyValue("Win Rate", `${overall.winRate}%`),
    formatKeyValue("Cumulative PnL", `${overall.pnl.toFixed(2)} pUSD`),
  ]);
}

async function buildPositionsDashboard(userId: string, user: any) {
  const trackedOpenPositions = db.getUnsettledTradeCount(userId);
  const claimableTrades = db.getClaimableTrades(userId);
  const paperStats = db.getPaperStats(userId);

  if (!hasImportedWallet(user)) {
    const lines = [
      "\u{1F3AF} Position Center",
      "",
      "Overview",
      formatKeyValue("Open Positions", trackedOpenPositions),
      formatKeyValue("Working Orders", 0),
      formatKeyValue("Claimable", claimableTrades.length),
      formatKeyValue("Auto-Claim", user.auto_claim ? "ON" : "OFF"),
      formatKeyValue("Paper Testing", user.paper_testing_active ? "ON" : "OFF"),
      "",
      "Wallet",
      formatKeyValue("Live Wallet", "not attached"),
      "",
      ...buildPaperStatsLines(paperStats).map((line) => line.replace(/\*/g, "")),
    ];

    if (trackedOpenPositions === 0) {
      lines.push("", "Activity", "No open positions or working orders right now.");
    }

    return {
      text: wrapCodeBlock(lines),
      keyboard: buildPositionsKeyboard(!!user?.auto_claim, claimableTrades.length > 0),
    };
  }

  const accountConfig = resolveUserPolymarketAccountConfig(user);
  const poly = new PolyMarketAPI({
    key: user.api_key,
    secret: user.api_secret,
    passphrase: user.api_passphrase
  }, user.private_key, accountConfig);

  const positionsAddress = accountConfig.funderAddress
    || privateKeyToAccount(user.private_key.startsWith("0x") ? user.private_key : `0x${user.private_key}`).address;

  const [, openOrders] = await Promise.all([
    poly.getPositions(positionsAddress),
    poly.getOpenOrders(),
  ]);
  const lines = [
    "\u{1F3AF} Position Center",
    "",
    "Overview",
    formatKeyValue("Open Positions", trackedOpenPositions),
    formatKeyValue("Working Orders", openOrders?.length || 0),
    formatKeyValue("Claimable", claimableTrades.length),
    formatKeyValue("Auto-Claim", user.auto_claim ? "ON" : "OFF"),
    formatKeyValue("Paper Testing", user.paper_testing_active ? "ON" : "OFF"),
    "",
    "Live Wallet",
    formatKeyValue("Dashboard Wallet", truncateMiddle(positionsAddress, 10, 6)),
    "",
    ...buildPaperStatsLines(paperStats).map((line) => line.replace(/\*/g, "")),
  ];

  if (openOrders && openOrders.length > 0) {
    lines.push("", "Working Orders");
    openOrders.slice(0, DASHBOARD_ORDERS_LIMIT).forEach((order: any) => {
      lines.push(formatOrderSummary(order));
    });
    if (openOrders.length > DASHBOARD_ORDERS_LIMIT) {
      lines.push(`Showing ${DASHBOARD_ORDERS_LIMIT} of ${openOrders.length} orders.`);
    }
  }

  if (trackedOpenPositions === 0 && (!openOrders || openOrders.length === 0)) {
    lines.push("", "Activity", "No open positions or working orders right now.");
  }

  return {
    text: wrapCodeBlock(lines),
    keyboard: buildPositionsKeyboard(!!user.auto_claim, claimableTrades.length > 0),
  };
}

async function buildActiveOrdersPage(userId: string, user: any) {
  if (!hasImportedWallet(user)) {
    return {
      text: wrapCodeBlock([
        "Active Market Orders",
        "",
        "Import a live wallet first to view active orders.",
      ]),
      keyboard: buildPositionsKeyboard(!!user?.auto_claim, false),
    };
  }

  const accountConfig = resolveUserPolymarketAccountConfig(user);
  const poly = new PolyMarketAPI({
    key: user.api_key,
    secret: user.api_secret,
    passphrase: user.api_passphrase
  }, user.private_key, accountConfig);

  const openOrders = await poly.getOpenOrders();
  const lines = [
    "Active Market Orders",
    "",
    formatKeyValue("Count", openOrders?.length || 0),
  ];

  if (openOrders && openOrders.length > 0) {
    lines.push("");
    openOrders.slice(0, DASHBOARD_ORDERS_LIMIT).forEach((order: any) => {
      lines.push(formatOrderSummary(order));
    });
    if (openOrders.length > DASHBOARD_ORDERS_LIMIT) {
      lines.push(`Showing ${DASHBOARD_ORDERS_LIMIT} of ${openOrders.length} orders.`);
    }
  } else {
    lines.push("", "No active market orders right now.");
  }

  return {
    text: wrapCodeBlock(lines),
    keyboard: buildPositionsKeyboard(!!user?.auto_claim, false),
  };
}

async function buildTradeHistoryPage(userId: string, user: any) {
  const realAll = db.getTradesForUser(userId);
  const paperAll = db.getPaperTradesForUser(userId);
  const realTrades = realAll.slice(0, DASHBOARD_POSITIONS_LIMIT);
  const paperTrades = paperAll.slice(0, DASHBOARD_POSITIONS_LIMIT);
  const lines = [
    "Trade History",
    "",
  ];

  if (realTrades.length > 0) {
    lines.push("Real Trades");
    realTrades.forEach((trade: any) => lines.push(formatRealTradeHistory(trade)));
    if (realAll.length > DASHBOARD_POSITIONS_LIMIT) {
      lines.push(`Showing ${DASHBOARD_POSITIONS_LIMIT} of ${realAll.length} real trades.`);
    }
    lines.push("");
  } else {
    lines.push("Real Trades", "No real trades yet.", "");
  }

  if (paperTrades.length > 0) {
    lines.push("Paper Trades");
    paperTrades.forEach((trade: any) => lines.push(formatPaperTradeHistory(trade)));
    if (paperAll.length > DASHBOARD_POSITIONS_LIMIT) {
      lines.push(`Showing ${DASHBOARD_POSITIONS_LIMIT} of ${paperAll.length} paper trades.`);
    }
  } else {
    lines.push("Paper Trades", "No paper trades yet.");
  }

  return {
    text: wrapCodeBlock(lines),
    keyboard: buildPositionsKeyboard(!!user?.auto_claim, false),
  };
}

function withDashboardNotice(baseText: string, notice?: string) {

  if (!notice) return baseText;
  return [`Dashboard Update`, notice, "", baseText].join("\n");
}

async function renderDashboardPage(userId: string, user: any, page: string, notice?: string) {
  const claimableTrades = user ? db.getClaimableTrades(userId) : [];
  const autoClaim = !!user?.auto_claim;
  const hasClaimables = claimableTrades.length > 0;

  if (!user) {
    if (["refresh", "main", "welcome", "claimable", "balance", "status", "stats", "daily", "controls", "risk_settings", "wallet_check", "orders", "history"].includes(page)) {
      return {
        text: withDashboardNotice(buildWelcomeDashboard(), notice || "No wallet profile found yet. Choose Real Trade or Paper Trade to begin."),
        keyboard: buildOnboardingKeyboard(),
      };
    }

    if (page === "help") {
      return {
        text: withDashboardNotice([
          "*Dashboard Help*",
          "",
          "Use /start to open this dashboard anytime.",
          "Use Setup Center to begin wallet import and onboarding.",
          "",
          "*Commands Still Useful*",
          "/import",
          "/approve",
          "/check_wallets",
          "/fund_funder <amt>",
          "/set_risk <%>",
          "/set_max <amt>",
          "/set_max_open <count>",
        ].join("\n"), notice),
        keyboard: buildOnboardingKeyboard(),
      };
    }

    return {
      text: withDashboardNotice(page === "setup" ? buildSetupMessage() : buildWelcomeDashboard(), notice),
      keyboard: buildOnboardingKeyboard(),
    };
  }

  if (page === "welcome") {
    return {
      text: withDashboardNotice(buildWelcomeDashboard(), notice),
      keyboard: buildOnboardingKeyboard(),
    };
  }

  if (page === "refresh" || page === "main") {
    const dashboard = await buildPositionsDashboard(userId, user);
    return {
      text: withDashboardNotice(dashboard.text, notice),
      keyboard: dashboard.keyboard,
    };
  }

  if (page === "orders") {
    return {
      text: withDashboardNotice((await buildActiveOrdersPage(userId, user)).text, notice),
      keyboard: buildPositionsKeyboard(!!user?.auto_claim, false),
    };
  }

  if (page === "history") {
    return {
      text: withDashboardNotice((await buildTradeHistoryPage(userId, user)).text, notice),
      keyboard: buildPositionsKeyboard(!!user?.auto_claim, false),
    };
  }

  if (page === "setup") {
    return {
      text: withDashboardNotice(buildSetupMessage(user), notice),
      keyboard: buildSetupKeyboard(true, hasImportedWallet(user)),
    };
  }

  if (page === "claimable") {
    return {
      text: withDashboardNotice(buildClaimableMessage(claimableTrades), notice),
      keyboard: buildClaimableKeyboard(claimableTrades, autoClaim),
    };
  }

  if (page === "balance") {
    return {
      text: withDashboardNotice(await buildBalanceMessage(user), notice),
      keyboard: buildDetailKeyboard(autoClaim, hasClaimables, "balance"),
    };
  }

  if (page === "status") {
    return {
      text: withDashboardNotice(buildStatusMessage(user), notice),
      keyboard: buildDetailKeyboard(autoClaim, hasClaimables, "status"),
    };
  }

  if (page === "controls") {
    return {
      text: withDashboardNotice(buildControlsMessage(user), notice),
      keyboard: buildControlsKeyboard(user),
    };
  }

  if (page === "risk_settings") {
    return {
      text: withDashboardNotice(buildRiskSettingsMessage(user), notice),
      keyboard: buildRiskKeyboard(),
    };
  }

  if (page === "wallet_check") {
    return {
      text: withDashboardNotice(await buildWalletCheckMessage(user), notice),
      keyboard: buildSetupKeyboard(true, hasImportedWallet(user)),
    };
  }

  if (page === "stats") {
    const overall = db.getOverallStats(userId);
    const paperStats = db.getPaperStats(userId);
    return {
      text: withDashboardNotice(
        (overall.total === 0 && paperStats.total === 0)
          ? "*Overall Performance*\n\n_No trades recorded yet._"
          : buildStatsMessage(overall, paperStats),
        notice
      ),
      keyboard: buildDetailKeyboard(autoClaim, hasClaimables, "stats"),
    };
  }

  if (page === "daily") {
    const daily = db.getDailyStats(userId);
    const overall = db.getOverallStats(userId);
    return {
      text: withDashboardNotice(buildDailyMessage(daily, overall), notice),
      keyboard: buildDetailKeyboard(autoClaim, hasClaimables, "daily"),
    };
  }

  if (page === "help") {
    return {
      text: withDashboardNotice([
        "*Dashboard Help*",
        "",
        "Use this dashboard as the main control center for positions, claims, balances, and reports.",
        "",
        "*Grouped Actions*",
        "Portfolio: Refresh, Balance, Status",
        "Claims: Claimable, Claim All, Auto-Claim toggle",
        "Reports: Stats, Daily",
        "",
        "*Use Commands For*",
        "Setup, approvals, funding, wallet checks, and risk-setting flows that need manual input.",
      ].join("\n"), notice),
      keyboard: buildDetailKeyboard(autoClaim, hasClaimables, "help"),
    };
  }

  return renderDashboardPage(userId, user, "main", notice);
}

async function safeEditDashboardMessage(ctx: any, text: string, keyboard: InlineKeyboard) {
  try {
    await ctx.editMessageText(text, {
      parse_mode: "HTML",
      reply_markup: keyboard,
    });
    return true;
  } catch (e: any) {
    const message = String(e?.message || "");
    if (message.includes("message is not modified")) {
      return false;
    }
    throw e;
  }
}

function buildClaimableMessage(claimableTrades: any[]) {
  if (claimableTrades.length === 0) {
    return wrapCodeBlock([
      "Claim Center",
      "",
      "No settled winning trades are waiting to be claimed.",
    ]);
  }

  const totalSize = claimableTrades.reduce((sum, trade) => sum + Number(trade.size || 0), 0);
  const lines = [
    "Claim Center",
    "",
    "Overview",
    formatKeyValue("Claimable Markets", claimableTrades.length),
    formatKeyValue("Claimable Size", totalSize.toFixed(2)),
    "",
    "Ready To Claim",
  ];
  claimableTrades.slice(0, CLAIMABLE_BUTTON_LIMIT).forEach((trade, index) => {
    lines.push(
      `${index + 1}. ${trade.market_id}`,
      formatKeyValue("Side", trade.side),
      formatKeyValue("Size", trade.size),
      formatKeyValue("Entry", trade.buy_price.toFixed(4)),
      ""
    );
  });

  if (claimableTrades.length > CLAIMABLE_BUTTON_LIMIT) {
    lines.push(`Showing ${CLAIMABLE_BUTTON_LIMIT} of ${claimableTrades.length} claimable markets.`);
  }

  return wrapCodeBlock(lines.map((line) => String(line))).trim();
}

async function claimMarketForUser(userId: string, user: any, marketId: string) {
  const trades = db.getClaimableTradesForMarket(userId, marketId);
  if (trades.length === 0) {
    throw new Error("No settled winning trade is waiting to be claimed for that market.");
  }

  const trade = trades[0];
  if (!trade.condition_id) {
    throw new Error("This trade is missing a condition id, so the bot cannot redeem it automatically.");
  }

  const accountConfig = resolveUserPolymarketAccountConfig(user);
  const poly = new PolyMarketAPI({
    key: user.api_key,
    secret: user.api_secret,
    passphrase: user.api_passphrase
  }, user.private_key, accountConfig);

  const txHash = await poly.redeemWinnings(trade.condition_id);
  db.markClaimedByCondition(userId, trade.condition_id, txHash);
  return txHash;
}

async function claimTradeByIdForUser(userId: string, user: any, tradeId: number) {
  const claimableTrades = db.getClaimableTrades(userId);
  const trade = claimableTrades.find((item: any) => Number(item.id) === tradeId);
  if (!trade) {
    throw new Error("No settled winning trade is waiting to be claimed for that selection.");
  }
  if (!trade.condition_id) {
    throw new Error("This trade is missing a condition id, so the bot cannot redeem it automatically.");
  }

  const accountConfig = resolveUserPolymarketAccountConfig(user);
  const poly = new PolyMarketAPI({
    key: user.api_key,
    secret: user.api_secret,
    passphrase: user.api_passphrase
  }, user.private_key, accountConfig);

  const txHash = await poly.redeemWinnings(trade.condition_id);
  db.markClaimedByCondition(userId, trade.condition_id, txHash);
  return txHash;
}

async function claimAllForUser(userId: string, user: any) {
  const claimableTrades = db.getClaimableTrades(userId);
  if (claimableTrades.length === 0) {
    return "No settled winning trades are waiting to be claimed.";
  }

  const uniqueConditions = Array.from(
    new Map(
      claimableTrades
        .filter((trade) => !!trade.condition_id)
        .map((trade) => [trade.condition_id, trade])
    ).values()
  );

  if (uniqueConditions.length === 0) {
    return "Claimable trades were found, but none have a usable condition id.";
  }

  const poly = new PolyMarketAPI({
    key: user.api_key,
    secret: user.api_secret,
    passphrase: user.api_passphrase
  }, user.private_key, resolveUserPolymarketAccountConfig(user));

  const receipts: string[] = [];
  const failures: string[] = [];

  for (const trade of uniqueConditions) {
    try {
      const txHash = await poly.redeemWinnings(trade.condition_id);
      db.markClaimedByCondition(userId, trade.condition_id, txHash);
      receipts.push(`${trade.market_id}: https://polygonscan.com/tx/${txHash}`);
    } catch (e: any) {
      failures.push(`${trade.market_id}: ${e.message}`);
    }
  }

  const lines = [];
  if (receipts.length > 0) {
    lines.push(`Claims submitted: ${receipts.length}`);
    lines.push(...receipts);
  }
  if (failures.length > 0) {
    lines.push(`Failures: ${failures.length}`);
    lines.push(...failures);
  }

  return lines.join("\n");
}

bot.catch((err) => {
  console.error(`[BOT ERROR] update ${err.ctx.update.update_id}:`, err.error);
});

bot.use(session({ initial: () => ({ step: "" }) }));

bot.command("start", async (ctx) => {
  console.log(`[BOT] User ${ctx.from?.id} ran /start`);
  if (!ctx.from) return;
  const user: any = db.getUser(ctx.from.id.toString());
  const view = await renderDashboardPage(ctx.from.id.toString(), user, "welcome");
  ctx.reply(view.text, {
    parse_mode: "HTML",
    reply_markup: view.keyboard,
  });
});

bot.command("help", (ctx) => {
  console.log(`[BOT] User ${ctx.from?.id} ran /help`);
  ctx.reply([
    "*Blocky Help*",
    "",
    "Use /start as the main dashboard for portfolio, claims, balances, reports, and setup.",
    "",
    "*Setup*",
    "/import",
    "/approve",
    "/check_wallets",
    "/fund_funder <amt>",
    "",
    "*Trading Controls*",
    "/start_trading",
    "/stop_trading",
    "/set_risk <%>",
    "/set_max <amt>",
    "/set_max_open <count>",
    "",
    "*Account*",
    "/remove_wallet",
  ].join("\n"), { parse_mode: "Markdown" });
});

bot.command("stats", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /stats`);
  const overall = db.getOverallStats(ctx.from.id.toString());
  const paperStats = db.getPaperStats(ctx.from.id.toString());

  if (overall.total === 0 && paperStats.total === 0) return ctx.reply("No trades recorded yet.");

  ctx.reply(buildStatsMessage(overall, paperStats), { parse_mode: "HTML" });
});

bot.command("daily", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /daily`);
  const daily = db.getDailyStats(ctx.from.id.toString());
  const overall = db.getOverallStats(ctx.from.id.toString());

  ctx.reply(buildDailyMessage(daily, overall), { parse_mode: "HTML" });
});

bot.command("import", async (ctx) => {
  console.log(`[BOT] User ${ctx.from?.id} ran /import`);
  ctx.reply(
    "Send your private key in this private chat.\n" +
    "Warning: use a dedicated hot wallet only. Your message will be deleted after processing."
  );
  ctx.session.step = "awaiting_pk";
  ctx.session.pending_private_key = "";
  ctx.session.pending_funder_address = "";
});

bot.command("status", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /status`);
  const user: any = db.getUser(ctx.from.id.toString());
  if (!user) return ctx.reply("User data not found. Use /import first.");

  ctx.reply(buildStatusMessage(user), { parse_mode: "HTML" });
});

bot.command("balance", async (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /balance`);
  const user: any = db.getUser(ctx.from.id.toString());
  if (!user) return ctx.reply("Use /import first.");
  if (!hasImportedWallet(user)) return ctx.reply("No live wallet is attached. Import one to check onchain balance.");

  try {
    ctx.reply(await buildBalanceMessage(user), { parse_mode: "HTML" });
  } catch (e: any) {
    console.error(`[BOT] Balance Error: ${e.message}`);
    ctx.reply(`Balance Error: ${e.message}`);
  }
});

bot.command("fund_funder", async (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /fund_funder ${ctx.match}`);
  const user: any = db.getUser(ctx.from.id.toString());
  if (!user) return ctx.reply("Use /import first.");
  if (!hasImportedWallet(user)) return ctx.reply("No live wallet is attached. Import one to fund the trading wallet.");

  const amount = parseFloat((ctx.match || "").trim());
  if (!Number.isFinite(amount) || amount <= 0) {
    return ctx.reply("Provide an amount in pUSD. Example: /fund_funder 25");
  }

  try {
    const accountConfig = resolveUserPolymarketAccountConfig(user);
    const poly = new PolyMarketAPI({
      key: user.api_key,
      secret: user.api_secret,
      passphrase: user.api_passphrase
    }, user.private_key, accountConfig);

    const txHash = await poly.transferPusdToFunder(amount);
    ctx.reply(
      `Signer-to-funder transfer submitted for ${amount.toFixed(2)} pUSD.\n` +
      `Only use this if you intentionally want to move Polygon pUSD into your Polymarket trading wallet.\n` +
      `https://polygonscan.com/tx/${txHash}`
    );
  } catch (e: any) {
    console.error(`[BOT] fund_funder Error: ${e.message}`);
    ctx.reply(`Funding transfer failed: ${e.message}`);
  }
});

bot.command("check_wallets", async (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /check_wallets`);
  const user: any = db.getUser(ctx.from.id.toString());
  if (!user) return ctx.reply("Use /import first.");
  if (!hasImportedWallet(user)) return ctx.reply("No live wallet is attached. Import one to run wallet checks.");

  try {
    const accountConfig = resolveUserPolymarketAccountConfig(user);
    const poly = new PolyMarketAPI({
      key: user.api_key,
      secret: user.api_secret,
      passphrase: user.api_passphrase
    }, user.private_key, accountConfig);

    const signerAddress = poly.getSignerAddress();
    const funderAddress = poly.getConfiguredFunderAddress();
    const profile = await poly.getPublicProfileByWallet(funderAddress);
    const proxyWallet = profile?.proxyWallet || null;

    ctx.reply(
      [
        "*Wallet Check*",
        "",
        `Signer: \`${signerAddress}\``,
        `Configured Funder: \`${funderAddress}\``,
        `Profile Proxy Wallet: \`${proxyWallet || "not found"}\``,
        `Signature Type: ${accountConfig.signatureType ?? "default(EOA)"}`,
        "",
        proxyWallet && proxyWallet.toLowerCase() === funderAddress.toLowerCase()
          ? "_Funder matches Polymarket profile proxy wallet._"
          : "_Funder does not match the Polymarket profile proxy wallet, or no profile was found._",
      ].join("\n"),
      { parse_mode: "Markdown" }
    );
  } catch (e: any) {
    console.error(`[BOT] check_wallets Error: ${e.message}`);
    ctx.reply(`Wallet check failed: ${e.message}`);
  }
});

bot.command("approve", async (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /approve`);
  const user: any = db.getUser(ctx.from.id.toString());
  if (!user) return ctx.reply("Use /import first.");
  if (!hasImportedWallet(user)) return ctx.reply("No live wallet is attached. Import one before sending approvals.");

  try {
    const accountConfig = resolveUserPolymarketAccountConfig(user);
    const poly = new PolyMarketAPI({
      key: user.api_key,
      secret: user.api_secret,
      passphrase: user.api_passphrase
    }, user.private_key, accountConfig);

    ctx.reply("Sending master approvals. This may take a few seconds.");
    const hashes = await poly.approveCollateral();
    const links = hashes.map((hash, index) => `Tx ${index + 1}: https://polygonscan.com/tx/${hash}`);
    ctx.reply(`Master approval successful.\n\n${links.join("\n")}\n\nYou can now check your status with /balance in a minute.`);
  } catch (e: any) {
    console.error(`[BOT] Approval Error: ${e.message}`);
    ctx.reply(`Approval failed: ${e.message}`);
  }
});

bot.command("positions", async (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /positions`);
  const user: any = db.getUser(ctx.from.id.toString());

  try {
    const view = await renderDashboardPage(ctx.from.id.toString(), user, "main");
    ctx.reply(view.text, {
      parse_mode: "HTML",
      reply_markup: view.keyboard,
    });
  } catch (e: any) {
    console.error(`[BOT] Positions Error: ${e.message}`);
    ctx.reply(`Positions error: ${e.message}`);
  }
});

bot.command("claimable", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /claimable`);

  const user: any = db.getUser(ctx.from.id.toString());
  if (!user) return ctx.reply("Use /import first.");

  const claimableTrades = db.getClaimableTrades(ctx.from.id.toString());
  if (claimableTrades.length === 0) {
    return ctx.reply("No settled winning trades are waiting to be claimed.");
  }

  ctx.reply(buildClaimableMessage(claimableTrades), {
    parse_mode: "HTML",
    reply_markup: buildClaimableKeyboard(claimableTrades, !!user.auto_claim),
  });
});

bot.command("claim", async (ctx) => {
  if (!ctx.from) return;
  const marketId = (ctx.match || "").trim();
  console.log(`[BOT] User ${ctx.from.id} ran /claim ${marketId}`);

  if (!marketId) {
    return ctx.reply("Provide a market id from your settled trade. Example: /claim 12345");
  }

  const user: any = db.getUser(ctx.from.id.toString());
  if (!user) return ctx.reply("Use /import first.");
  if (!hasImportedWallet(user)) return ctx.reply("No live wallet is attached. Import one before submitting claims.");

  try {
    const txHash = await claimMarketForUser(ctx.from.id.toString(), user, marketId);
    ctx.reply(`Claim submitted: https://polygonscan.com/tx/${txHash}`);
  } catch (e: any) {
    console.error(`[BOT] Claim Error: ${e.message}`);
    ctx.reply(`Claim failed: ${e.message}`);
  }
});

bot.command("claim_all", async (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /claim_all`);

  const user: any = db.getUser(ctx.from.id.toString());
  if (!user) return ctx.reply("Use /import first.");
  if (!hasImportedWallet(user)) return ctx.reply("No live wallet is attached. Import one before claiming winnings.");

  ctx.reply(await claimAllForUser(ctx.from.id.toString(), user));
});

bot.command("auto_claim_on", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /auto_claim_on`);
  db.updateAutoClaim(ctx.from.id.toString(), true);
  ctx.reply("Auto-claim enabled. Winning settled trades will be redeemed automatically when possible.");
});

bot.command("auto_claim_off", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /auto_claim_off`);
  db.updateAutoClaim(ctx.from.id.toString(), false);
  ctx.reply("Auto-claim disabled. Use /claimable, /claim, or /claim_all to redeem winners manually.");
});

bot.command("set_risk", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /set_risk ${ctx.match}`);
  const reqRisk = ctx.match || "";
  const risk = parseFloat(reqRisk);
  if (isNaN(risk) || risk <= 0 || risk > 100) {
    return ctx.reply("Please provide a percentage from 1-100. Example: /set_risk 5");
  }
  db.updateRisk(ctx.from.id.toString(), risk);
  ctx.reply(`Risk set to ${risk}% per trade.`);
});

bot.command("set_max", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /set_max ${ctx.match}`);
  const reqMax = ctx.match || "";
  const max = parseFloat(reqMax);
  if (isNaN(max) || max <= 0) {
    return ctx.reply("Please provide a valid amount. Example: /set_max 50");
  }
  db.updateMaxTrade(ctx.from.id.toString(), max);
  ctx.reply(`Max trade amount set to $${max}.`);
});

bot.command("set_max_open", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /set_max_open ${ctx.match}`);
  const reqMaxOpen = ctx.match || "";
  const maxOpen = parseInt(reqMaxOpen, 10);
  if (isNaN(maxOpen) || maxOpen <= 0) {
    return ctx.reply("Please provide a whole number greater than 0. Example: /set_max_open 10");
  }
  db.updateMaxOpenPositions(ctx.from.id.toString(), maxOpen);
  ctx.reply(`Maximum concurrent open positions set to ${maxOpen}.`);
});

bot.command("start_trading", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /start_trading`);
  const user: any = db.getUser(ctx.from.id.toString());
  if (!user || !hasImportedWallet(user)) {
    return ctx.reply("Import a live wallet before enabling real auto-trading.");
  }
  db.updateTradingStatus(ctx.from.id.toString(), true);
  ctx.reply("Auto-trading enabled.");
});

bot.command("stop_trading", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /stop_trading`);
  db.updateTradingStatus(ctx.from.id.toString(), false);
  ctx.reply("Auto-trading disabled.");
});

bot.command("remove_wallet", (ctx) => {
  if (!ctx.from) return;
  console.log(`[BOT] User ${ctx.from.id} ran /remove_wallet`);
  db.clearUserWallet(ctx.from.id.toString());
  ctx.reply("Live wallet credentials were removed. Your profile, settings, and paper-testing data were kept.");
});

bot.callbackQuery(/^positions:(.+)$/, async (ctx) => {
  if (!ctx.from) return;
  const action = ctx.match[1];
  const userId = ctx.from.id.toString();
  const user: any = db.getUser(userId);

  try {
    if (["main", "refresh", "setup", "help", "welcome"].includes(action)) {
      const view = await renderDashboardPage(userId, user, action);
      const changed = await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({
        text: changed
          ? (action === "refresh" ? "Dashboard refreshed." : "Dashboard updated.")
          : "Already up to date.",
      });
      return;
    }

    if (action === "import_start") {
      ctx.session.step = "awaiting_pk";
      ctx.session.pending_private_key = "";
      ctx.session.pending_funder_address = "";
      const view = await renderDashboardPage(userId, user, "setup", "Wallet import started. Send your private key in this chat.");
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.reply(
        "Send your private key in this private chat.\n" +
        "Warning: use a dedicated hot wallet only. Your message will be deleted after processing."
      );
      await ctx.answerCallbackQuery({ text: "Import flow started." });
      return;
    }

    if (action === "real_trade") {
      if (user?.paper_testing_active) {
        db.updatePaperTestingStatus(userId, false);
      }
      const updatedUser: any = db.getUser(userId);
      const view = await renderDashboardPage(
        userId,
        updatedUser,
        "setup",
        "Real Trade selected. Import a wallet, approve pUSD, and fund the trading wallet to continue."
      );
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: "Real trade selected." });
      return;
    }

    if (action === "paper_trade") {
      db.updatePaperTestingStatus(userId, true);
      const updatedUser: any = db.getUser(userId);
      const view = await renderDashboardPage(
        userId,
        updatedUser,
        "controls",
        "Paper Trade selected. Paper testing is now enabled."
      );
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: "Paper trade selected." });
      return;
    }

    if (!user) {
      const fallbackView = await renderDashboardPage(userId, user, "setup", "No wallet profile found yet.");
      await safeEditDashboardMessage(ctx, fallbackView.text, fallbackView.keyboard);
      await ctx.answerCallbackQuery({ text: "Open Setup to get started." });
      return;
    }

    if (["claimable", "balance", "status", "stats", "daily", "controls", "risk_settings", "wallet_check", "orders", "history"].includes(action)) {
      if (["balance", "wallet_check"].includes(action) && !hasImportedWallet(user)) {
        await ctx.answerCallbackQuery({ text: "Import a live wallet for that action.", show_alert: true });
        return;
      }
      const view = await renderDashboardPage(userId, user, action);
      const changed = await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: changed ? "Dashboard updated." : "Already up to date." });
      return;
    }

    if (action === "approve") {
      if (!hasImportedWallet(user)) {
        await ctx.answerCallbackQuery({ text: "Import a live wallet first.", show_alert: true });
        return;
      }
      const accountConfig = resolveUserPolymarketAccountConfig(user);
      const poly = new PolyMarketAPI({
        key: user.api_key,
        secret: user.api_secret,
        passphrase: user.api_passphrase
      }, user.private_key, accountConfig);

      const hashes = await poly.approveCollateral();
      const links = hashes.map((hash, index) => `Tx ${index + 1}: https://polygonscan.com/tx/${hash}`).join("\n");
      const view = await renderDashboardPage(userId, user, "setup", `Approvals submitted.\n${links}`);
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: "Approvals submitted." });
      return;
    }

    if (action === "fund_prompt") {
      if (!hasImportedWallet(user)) {
        await ctx.answerCallbackQuery({ text: "Import a live wallet first.", show_alert: true });
        return;
      }
      ctx.session.step = "awaiting_fund_amount";
      const view = await renderDashboardPage(userId, user, "setup", "Funding prompt opened. Send the pUSD amount to move.");
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.reply("Send the amount of pUSD to move into the trading wallet. Example: `25`", { parse_mode: "Markdown" });
      await ctx.answerCallbackQuery({ text: "Send funding amount." });
      return;
    }

    if (action === "risk_prompt") {
      ctx.session.step = "awaiting_set_risk";
      const view = await renderDashboardPage(userId, user, "risk_settings", "Risk update opened. Send the new percentage.");
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.reply("Send the new risk percentage from 1-100. Example: `5`", { parse_mode: "Markdown" });
      await ctx.answerCallbackQuery({ text: "Send risk %." });
      return;
    }

    if (action === "max_prompt") {
      ctx.session.step = "awaiting_set_max";
      const view = await renderDashboardPage(userId, user, "risk_settings", "Max trade update opened. Send the new amount.");
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.reply("Send the new max trade amount. Example: `50`", { parse_mode: "Markdown" });
      await ctx.answerCallbackQuery({ text: "Send max trade." });
      return;
    }

    if (action === "max_open_prompt") {
      ctx.session.step = "awaiting_set_max_open";
      const view = await renderDashboardPage(userId, user, "risk_settings", "Max open update opened. Send the new count.");
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.reply("Send the new maximum open positions count. Example: `10`", { parse_mode: "Markdown" });
      await ctx.answerCallbackQuery({ text: "Send max open." });
      return;
    }

    if (action === "start_trading" || action === "stop_trading") {
      const enabled = action === "start_trading";
      if (enabled && !hasImportedWallet(user)) {
        await ctx.answerCallbackQuery({ text: "Import a live wallet before enabling real trading.", show_alert: true });
        return;
      }
      db.updateTradingStatus(userId, enabled);
      const updatedUser: any = db.getUser(userId);
      const view = await renderDashboardPage(
        userId,
        updatedUser,
        "controls",
        enabled ? "Auto-trading enabled." : "Auto-trading disabled."
      );
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: enabled ? "Trading enabled." : "Trading disabled." });
      return;
    }

    if (action === "claim_all") {
      if (!hasImportedWallet(user)) {
        await ctx.answerCallbackQuery({ text: "Import a live wallet before claiming.", show_alert: true });
        return;
      }
      const result = await claimAllForUser(userId, user);
      const updatedUser: any = db.getUser(userId);
      const view = await renderDashboardPage(userId, updatedUser, "claimable", result);
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: "Claim all processed." });
      return;
    }

    if (action === "auto_claim_on" || action === "auto_claim_off") {
      const enabled = action === "auto_claim_on";
      db.updateAutoClaim(userId, enabled);
      const updatedUser: any = db.getUser(userId);
      const view = await renderDashboardPage(
        userId,
        updatedUser,
        "refresh",
        enabled ? "Auto-claim enabled." : "Auto-claim disabled."
      );
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: enabled ? "Auto-claim enabled." : "Auto-claim disabled." });
      return;
    }

    if (action === "paper_testing_on" || action === "paper_testing_off") {
      const enabled = action === "paper_testing_on";
      db.updatePaperTestingStatus(userId, enabled);
      const updatedUser: any = db.getUser(userId);
      const view = await renderDashboardPage(
        userId,
        updatedUser,
        "controls",
        enabled ? "Paper signal testing enabled." : "Paper signal testing disabled."
      );
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: enabled ? "Paper testing enabled." : "Paper testing disabled." });
      return;
    }

    if (action === "remove_wallet") {
      db.clearUserWallet(userId);
      const updatedUser: any = db.getUser(userId);
      const view = await renderDashboardPage(
        userId,
        updatedUser,
        "setup",
        "Live wallet removed. Profile settings and paper-testing data were kept."
      );
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: "Wallet removed." });
      return;
    }

    if (action.startsWith("claim:")) {
      if (!hasImportedWallet(user)) {
        await ctx.answerCallbackQuery({ text: "Import a live wallet before claiming.", show_alert: true });
        return;
      }
      const tradeId = Number.parseInt(action.slice("claim:".length), 10);
      if (!Number.isInteger(tradeId)) {
        throw new Error("Invalid claim selection.");
      }
      const txHash = await claimTradeByIdForUser(userId, user, tradeId);
      const updatedUser: any = db.getUser(userId);
      const view = await renderDashboardPage(
        userId,
        updatedUser,
        "claimable",
        `Claim submitted: https://polygonscan.com/tx/${txHash}`
      );
      await safeEditDashboardMessage(ctx, view.text, view.keyboard);
      await ctx.answerCallbackQuery({ text: "Claim submitted." });
      return;
    }

    await ctx.answerCallbackQuery({ text: "Unknown action." });
  } catch (e: any) {
    console.error(`[BOT] Callback Error (${action}): ${e.message}`);
    await ctx.answerCallbackQuery({ text: "Action failed.", show_alert: true });
  }
});

bot.on("message:text", async (ctx) => {
  if (ctx.session.step === "awaiting_pk") {
    if (!ctx.from) return;
    const pk = ctx.message.text.trim();
    try {
      await ctx.api.deleteMessage(ctx.chat.id, ctx.message.message_id);
    } catch (e: any) {
      console.warn(`[BOT] Could not delete sensitive import message for ${ctx.from.id}: ${e.message}`);
    }

    console.log(`[BOT] Processing wallet import for user ${ctx.from.id}...`);
    ctx.session.pending_private_key = pk;
    ctx.session.step = "awaiting_funder";
    ctx.reply(
      "Now send your Polymarket displayed wallet address.\n" +
      "If your Polymarket account uses the same wallet as the signer, reply `skip`.",
      { parse_mode: "Markdown" }
    );
    return;
  }

  if (ctx.session.step === "awaiting_funder") {
    if (!ctx.from) return;
    const rawValue = ctx.message.text.trim();
    const funderAddress = rawValue.toLowerCase() === "skip" ? null : rawValue;

    if (funderAddress && !/^0x[a-fA-F0-9]{40}$/.test(funderAddress)) {
      ctx.reply("That funder address does not look valid. Send a `0x...` wallet address or reply `skip`.");
      return;
    }

    ctx.session.pending_funder_address = funderAddress || "";
    ctx.session.step = "awaiting_signature_type";
    ctx.reply(
      "Reply with signature type:\n" +
      "`0` = same wallet / EOA\n" +
      "`1` = Polymarket email-Google proxy\n" +
      "`2` = Polymarket browser-wallet proxy (most common)",
      { parse_mode: "Markdown" }
    );
    return;
  }

  if (ctx.session.step === "awaiting_signature_type") {
    if (!ctx.from) return;
    const rawValue = ctx.message.text.trim();
    const signatureType = Number.parseInt(rawValue, 10);

    if (![0, 1, 2].includes(signatureType)) {
      ctx.reply("Reply with `0`, `1`, or `2`.", { parse_mode: "Markdown" });
      return;
    }

    const pk = ctx.session.pending_private_key || "";
    const funderAddress = ctx.session.pending_funder_address || null;

    try {
      const accountConfig = {
        funderAddress,
        signatureType,
      };
      const creds = await crypto.deriveApiKeys(pk, accountConfig);
      db.saveUser({
        tg_id: ctx.from.id.toString(),
        private_key: pk,
        api_key: creds.key,
        api_secret: creds.secret,
        api_passphrase: creds.passphrase,
        funder_address: accountConfig.funderAddress,
        signature_type: accountConfig.signatureType,
      });
      console.log(`[BOT] Successfully saved encrypted credentials for user ${ctx.from.id}`);
      ctx.reply("Wallet imported successfully. Sensitive credentials were encrypted at rest.");
      const savedUser: any = db.getUser(ctx.from.id.toString());
      const view = await renderDashboardPage(ctx.from.id.toString(), savedUser, "setup", "Wallet import completed.");
      ctx.reply(view.text, {
        parse_mode: "HTML",
        reply_markup: view.keyboard,
      });
      ctx.session.step = "";
      ctx.session.pending_private_key = "";
      ctx.session.pending_funder_address = "";
    } catch (e: any) {
      console.error(`[BOT] Import Error for ${ctx.from.id}.`);
      if (String(e?.message || "").includes("MASTER_ENCRYPTION_KEY")) {
        ctx.reply("Wallet import is temporarily unavailable because MASTER_ENCRYPTION_KEY is not configured on the server.");
      } else {
        ctx.reply("Wallet import failed. Please verify the private key and try again.");
      }
      ctx.session.step = "";
      ctx.session.pending_private_key = "";
      ctx.session.pending_funder_address = "";
    }
  }

  if (ctx.session.step === "awaiting_fund_amount") {
    if (!ctx.from) return;
    const user: any = db.getUser(ctx.from.id.toString());
    if (!user) {
      ctx.session.step = "";
      ctx.reply("Use /import first.");
      return;
    }

    const amount = parseFloat(ctx.message.text.trim());
    if (!Number.isFinite(amount) || amount <= 0) {
      ctx.reply("Send a valid pUSD amount. Example: `25`", { parse_mode: "Markdown" });
      return;
    }

    try {
      const accountConfig = resolveUserPolymarketAccountConfig(user);
      const poly = new PolyMarketAPI({
        key: user.api_key,
        secret: user.api_secret,
        passphrase: user.api_passphrase
      }, user.private_key, accountConfig);
      const txHash = await poly.transferPusdToFunder(amount);
      ctx.reply(
        `Signer-to-funder transfer submitted for ${amount.toFixed(2)} pUSD.\nhttps://polygonscan.com/tx/${txHash}`
      );
      ctx.session.step = "";
    } catch (e: any) {
      console.error(`[BOT] fund_funder Error: ${e.message}`);
      ctx.reply(`Funding transfer failed: ${e.message}`);
      ctx.session.step = "";
    }
    return;
  }

  if (ctx.session.step === "awaiting_set_risk") {
    if (!ctx.from) return;
    const risk = parseFloat(ctx.message.text.trim());
    if (isNaN(risk) || risk <= 0 || risk > 100) {
      ctx.reply("Send a percentage from 1-100. Example: `5`", { parse_mode: "Markdown" });
      return;
    }
    db.updateRisk(ctx.from.id.toString(), risk);
    ctx.session.step = "";
    ctx.reply(`Risk set to ${risk}% per trade.`);
    return;
  }

  if (ctx.session.step === "awaiting_set_max") {
    if (!ctx.from) return;
    const max = parseFloat(ctx.message.text.trim());
    if (isNaN(max) || max <= 0) {
      ctx.reply("Send a valid max trade amount. Example: `50`", { parse_mode: "Markdown" });
      return;
    }
    db.updateMaxTrade(ctx.from.id.toString(), max);
    ctx.session.step = "";
    ctx.reply(`Max trade amount set to $${max}.`);
    return;
  }

  if (ctx.session.step === "awaiting_set_max_open") {
    if (!ctx.from) return;
    const maxOpen = parseInt(ctx.message.text.trim(), 10);
    if (isNaN(maxOpen) || maxOpen <= 0) {
      ctx.reply("Send a whole number greater than 0. Example: `10`", { parse_mode: "Markdown" });
      return;
    }
    db.updateMaxOpenPositions(ctx.from.id.toString(), maxOpen);
    ctx.session.step = "";
    ctx.reply(`Maximum concurrent open positions set to ${maxOpen}.`);
    return;
  }
});

console.log("--------------------------");
console.log("Blocky Polymarket Bot Starting (Signer Support)...");
console.log("--------------------------");

const releaseLock = acquireProcessLock("telegram-bot");
if (!releaseLock) {
  process.exit(0);
}

bot.start().catch((err) => {
  releaseLock();
  console.error(err);
});
