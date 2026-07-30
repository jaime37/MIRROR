"""
Paper trading engine. Simulates order execution at real Polymarket prices
without spending real money.
"""

import threading
from datetime import datetime, timezone
from typing import Optional
from .portfolio_db import PortfolioDatabase
from ...utils.logger import get_logger

logger = get_logger("mirofish.polymarket.paper_trader")

FEE_RATE = 0.02  # legacy flat fee — kept exported for backward compatibility only
SLIPPAGE_FACTOR = 0.5  # slippage scales with order_size / liquidity
MAX_SLIPPAGE = 0.05  # cap slippage at 5%

# Real Polymarket taker fee rates by market category (fee = shares × rate × p × (1-p))
_CATEGORY_FEE_RATES = (
    ("geopolitics", 0.0),  # checked first: "geopolitics" contains "politics"
    ("crypto", 0.07),
    ("politics", 0.04),
    ("finance", 0.04),
    ("tech", 0.04),
    ("mentions", 0.04),
)
DEFAULT_FEE_RATE = 0.05  # unknown / other categories

# Module-level lock: ensures open/close operations are serialized across threads.
# Prevents duplicate CLOSE records when the background bot and a manual trigger
# run concurrently, or when two Railway processes start before the debounce fires.
_position_lock = threading.Lock()


def fee_rate_for_category(category: Optional[str]) -> float:
    """Returns the taker fee rate for a market category (substring match)."""
    cat = (category or "").lower()
    for key, rate in _CATEGORY_FEE_RATES:
        if key in cat:
            return rate
    return DEFAULT_FEE_RATE


def taker_fee(shares: float, price: float, category: Optional[str] = None) -> float:
    """Real Polymarket taker fee: shares × rate × price × (1 − price)."""
    return shares * fee_rate_for_category(category) * price * (1.0 - price)


def _apply_slippage(
    midpoint: float,
    amount_usdc: float,
    liquidity: Optional[float],
    action: str,  # "buy" or "sell"
) -> float:
    """
    Returns a conservative fill price given order size and market liquidity.
    Buying lifts the price; selling pushes it down. Low-liquidity markets pay
    more slippage, capped at MAX_SLIPPAGE.
    """
    if not liquidity or liquidity <= 0:
        return midpoint
    ratio = amount_usdc / liquidity
    slippage = min(MAX_SLIPPAGE, SLIPPAGE_FACTOR * ratio)
    if action == "buy":
        fill = midpoint * (1 + slippage)
    else:
        fill = midpoint * (1 - slippage)
    # Keep price in valid (0, 1) range
    return max(0.001, min(0.999, fill))


def _walk_book(levels: list, shares_needed: float) -> tuple[float, float]:
    """
    Walks up to 10 book levels and returns (vwap_price, filled_shares).
    If book depth is insufficient, the remainder is priced at the last level
    consumed (and a warning is logged).
    """
    filled = 0.0
    cost = 0.0
    last_price = 0.0
    for price, size in levels[:10]:
        take = min(size, shares_needed - filled)
        cost += take * price
        filled += take
        last_price = price
        if filled >= shares_needed:
            break
    if filled < shares_needed and last_price > 0:
        logger.warning(
            f"Book depth insufficient: filled {filled:.2f}/{shares_needed:.2f} shares — "
            f"pricing remainder at last level {last_price:.4f}"
        )
        cost += (shares_needed - filled) * last_price
        filled = shares_needed
    vwap = cost / filled if filled > 0 else 0.0
    return vwap, filled


class PaperTrader:
    def __init__(self):
        self.db = PortfolioDatabase()

    def open_position(
        self,
        market_id: str,
        question: str,
        side: str,           # "YES" or "NO"
        entry_price: float,  # current market price for chosen side
        amount_usdc: float,  # how much virtual USDC to spend
        estimated_prob: float,
        confidence: str,
        reasoning: str,
        simulation_id: Optional[str] = None,
        report_id: Optional[str] = None,
        end_date: Optional[str] = None,
        liquidity: Optional[float] = None,
        category: Optional[str] = None,
        execution_mode: str = "taker",   # "taker" or "maker"
        book: Optional[dict] = None,     # {"bids": [...], "asks": [...]} or None
        settings_version: str = "",
        entry_bucket: str = "",
    ) -> dict:
        """Opens a new paper position. Returns the trade record."""
        with _position_lock:
            return self._open_position_locked(
                market_id, question, side, entry_price, amount_usdc,
                estimated_prob, confidence, reasoning,
                simulation_id, report_id, end_date, liquidity, category,
                execution_mode, book, settings_version, entry_bucket,
            )

    def _open_position_locked(
        self,
        market_id: str,
        question: str,
        side: str,
        entry_price: float,
        amount_usdc: float,
        estimated_prob: float,
        confidence: str,
        reasoning: str,
        simulation_id: Optional[str] = None,
        report_id: Optional[str] = None,
        end_date: Optional[str] = None,
        liquidity: Optional[float] = None,
        category: Optional[str] = None,
        execution_mode: str = "taker",
        book: Optional[dict] = None,
        settings_version: str = "",
        entry_bucket: str = "",
    ) -> dict:
        """Internal: caller must hold _position_lock."""
        # Re-read from disk inside the lock so we see the latest state
        portfolio = self.db.get_portfolio()

        if amount_usdc > portfolio["balance"]:
            raise ValueError(
                f"Insufficient balance: {portfolio['balance']:.2f} USDC available, "
                f"{amount_usdc:.2f} requested"
            )

        if market_id in portfolio["positions"]:
            raise ValueError(f"Position already open for market {market_id}")

        # ── Fill price by execution mode ──────────────────────────────────────
        if execution_mode == "maker":
            # Limit-order assumption: buy at best bid, no fee. Without a book,
            # fill at the given mid price.
            if book and book.get("bids"):
                fill_price = book["bids"][0][0]
            else:
                fill_price = entry_price
            fee = 0.0
            shares = amount_usdc / fill_price if fill_price > 0 else 0
        else:
            # Taker: walk the asks if a book is available, else slippage model
            if book and book.get("asks"):
                est_shares = amount_usdc / entry_price if entry_price > 0 else 0
                fill_price, _ = _walk_book(book["asks"], est_shares)
                if fill_price <= 0:
                    fill_price = _apply_slippage(entry_price, amount_usdc, liquidity, action="buy")
            else:
                fill_price = _apply_slippage(entry_price, amount_usdc, liquidity, action="buy")
            rate = fee_rate_for_category(category)
            # Solve shares so that shares*fill + fee == amount_usdc exactly
            shares = amount_usdc / (fill_price * (1 + rate * (1 - fill_price))) if fill_price > 0 else 0
            fee = taker_fee(shares, fill_price, category)

        position = {
            "market_id": market_id,
            "question": question,
            "side": side,
            "entry_price": round(fill_price, 4),
            "midpoint_price": round(entry_price, 4),
            "category": category or "",
            "shares": round(shares, 4),
            "cost_basis": round(amount_usdc, 4),
            "fee_paid": round(fee, 4),
            "liquidity": liquidity or 0.0,
            "current_price": round(fill_price, 4),
            "current_value": round(shares * fill_price, 4),
            "unrealized_pnl": round(shares * fill_price - amount_usdc, 4),
            "estimated_prob": round(estimated_prob, 4),
            "confidence": confidence,
            "reasoning": reasoning,
            "simulation_id": simulation_id,
            "report_id": report_id,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "end_date": end_date or "",
            "status": "open",
        }

        portfolio["balance"] = round(portfolio["balance"] - amount_usdc, 4)
        portfolio["positions"][market_id] = position
        self.db.save_portfolio(portfolio)

        trade = self.db.add_trade({
            "type": "OPEN",
            "market_id": market_id,
            "question": question,
            "side": side,
            "price": round(fill_price, 4),
            "midpoint_price": round(entry_price, 4),
            "shares": round(shares, 4),
            "amount_usdc": round(amount_usdc, 4),
            "fee": round(fee, 4),
            "estimated_prob": round(estimated_prob, 4),
            "confidence": confidence,
            "reasoning": reasoning,
            "category": category or "",
            "simulation_id": simulation_id,
            "report_id": report_id,
            "settings_version": settings_version,
            "entry_bucket": entry_bucket,
            "execution_mode": execution_mode,
        })

        self._snapshot_equity()
        logger.info(
            f"Opened paper position: {side} on '{question[:60]}' "
            f"@ {fill_price:.4f} (mid {entry_price:.4f}) for {amount_usdc:.2f} USDC "
            f"[{execution_mode}]"
        )
        return trade

    def close_position(
        self,
        market_id: str,
        exit_price: float,
        reason: str = "manual",
        liquidity: Optional[float] = None,
        execution_mode: str = "taker",   # "taker" or "maker"
        book: Optional[dict] = None,     # {"bids": [...], "asks": [...]} or None
        payout: Optional[float] = None,  # binary resolution payout (0.0 or 1.0)
    ) -> dict:
        """Closes an open paper position at the given exit price."""
        with _position_lock:
            return self._close_position_locked(
                market_id, exit_price, reason, liquidity, execution_mode, book, payout
            )

    def _close_position_locked(
        self,
        market_id: str,
        exit_price: float,
        reason: str = "manual",
        liquidity: Optional[float] = None,
        execution_mode: str = "taker",
        book: Optional[dict] = None,
        payout: Optional[float] = None,
    ) -> dict:
        """Internal: caller must hold _position_lock."""
        # Re-read from disk inside the lock — another thread may have already
        # closed this position since the caller last checked.
        portfolio = self.db.get_portfolio()

        if market_id not in portfolio["positions"]:
            raise ValueError(f"No open position for market {market_id}")

        pos = portfolio["positions"][market_id]

        if payout is not None:
            # Binary resolution: ignore exit price/slippage/fees entirely
            fill_price = payout
            fee = 0.0
            proceeds = pos["shares"] * payout
        elif execution_mode == "maker":
            # Limit-order assumption: sell at best ask, no fee. Without a book,
            # fill at the given mid price.
            if book and book.get("asks"):
                fill_price = book["asks"][0][0]
            else:
                fill_price = exit_price
            fee = 0.0
            proceeds = pos["shares"] * fill_price
        else:
            close_liquidity = liquidity if liquidity is not None else pos.get("liquidity", 0)
            notional = pos["shares"] * exit_price
            # Taker: walk the bids if a book is available, else slippage model
            if book and book.get("bids"):
                fill_price, _ = _walk_book(book["bids"], pos["shares"])
                if fill_price <= 0:
                    fill_price = _apply_slippage(exit_price, notional, close_liquidity, action="sell")
            else:
                fill_price = _apply_slippage(exit_price, notional, close_liquidity, action="sell")
            fee = taker_fee(pos["shares"], fill_price, pos.get("category", ""))
            proceeds = pos["shares"] * fill_price - fee

        pnl = proceeds - pos["cost_basis"]

        # Remove position and update balance BEFORE writing the trade record.
        # This ordering ensures that if the process crashes between the two writes,
        # the position is gone from the portfolio (so it won't be double-closed on
        # the next restart) even if the CLOSE trade record is missing.
        portfolio["balance"] = round(portfolio["balance"] + proceeds, 4)
        del portfolio["positions"][market_id]
        self.db.save_portfolio(portfolio)

        trade = self.db.add_trade({
            "type": "CLOSE",
            "market_id": market_id,
            "question": pos["question"],
            "side": pos["side"],
            "entry_price": pos["entry_price"],
            "exit_price": round(fill_price, 4),
            "exit_midpoint": round(exit_price, 4),
            "shares": pos["shares"],
            "proceeds": round(proceeds, 4),
            "fee": round(fee, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / pos["cost_basis"] * 100, 2),
            "reason": reason,
            "opened_at": pos["opened_at"],
            "execution_mode": execution_mode,
        })

        self._snapshot_equity()
        logger.info(
            f"Closed paper position on '{pos['question'][:60]}': "
            f"fill {fill_price:.4f} (mid {exit_price:.4f}) "
            f"PnL = {pnl:+.2f} USDC ({pnl / pos['cost_basis'] * 100:+.1f}%) [{execution_mode}]"
        )
        return trade

    def update_position_price(self, market_id: str, current_price: float):
        """Updates unrealized P&L for an open position."""
        portfolio = self.db.get_portfolio()
        if market_id not in portfolio["positions"]:
            return

        pos = portfolio["positions"][market_id]
        current_value = pos["shares"] * current_price
        pos["current_price"] = round(current_price, 4)
        pos["current_value"] = round(current_value, 4)
        pos["unrealized_pnl"] = round(current_value - pos["cost_basis"], 4)
        portfolio["positions"][market_id] = pos
        self.db.save_portfolio(portfolio)

        # Record a mark for observability — must never break a cycle
        try:
            self.db.add_position_mark(market_id, current_price)
        except Exception:
            pass

    def _snapshot_equity(self):
        stats = self.db.get_stats()
        self.db.record_equity(stats["total_value"])
