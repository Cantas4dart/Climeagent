from __future__ import annotations

from typing import Iterable


PLATFORM_FEE_BPS = 100
PLATFORM_FEE_RATE = PLATFORM_FEE_BPS / 10_000
PLATFORM_FEE_RECIPIENT = "0x48c9fbF929Bb95F92fb27984D2eBf40554B42b7d"
PLATFORM_FEE_ADMIN_TG_ID = "2105500542"


def is_platform_fee_exempt(user_id: str | None) -> bool:
    return str(user_id or "") == PLATFORM_FEE_ADMIN_TG_ID


def calculate_platform_fee_from_pnl(pnl: float | int | None) -> float:
    """
    Deprecated: Use calculate_platform_fee_from_trade instead.
    Kept for backward compatibility.
    """
    try:
        profit = max(0.0, float(pnl or 0.0))
    except (TypeError, ValueError):
        return 0.0
    return round(profit * PLATFORM_FEE_RATE, 6)


def calculate_platform_fee_from_trade(trade: object) -> float:
    """
    Calculate platform fee as (capital + profit) * 0.01, only for winning trades.
    Capital = buy_price * size
    Profit = pnl
    Only charge fee if pnl > 0 (winning trades).
    """
    try:
        buy_price = float(getattr(trade, "buy_price", 0.0) or 0.0)
        size = float(getattr(trade, "size", 0.0) or 0.0)
        pnl = float(getattr(trade, "pnl", 0.0) or 0.0)
        
        # Only charge fee on winning trades
        if pnl <= 0:
            return 0.0
        
        capital = buy_price * size
        total = capital + pnl
        
        return round(total * PLATFORM_FEE_RATE, 6)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def calculate_total_platform_fee(trades: Iterable[object], user_id: str | None) -> float:
    if is_platform_fee_exempt(user_id):
        return 0.0
    return round(sum(calculate_platform_fee_from_trade(trade) for trade in trades), 6)
