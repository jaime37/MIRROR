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

FEE_RATE = 0.02  # 2% simulated fee per trade
SLIPPAGE_FACTOR = 0.5  # slippage scales with order_size / liquidity
MAX_SLIPPAGE = 0.05  # cap slippage at 5%

# Module-level lock: ensures open/close operations are serialized across threads.
# Prevents duplicate CLOSE records when the background bot and a manual trigger
# run concurrently, or when two Railway processes start before the debounce fires.
_position_lock = threading.Lock()


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
    ) -> dict:
        """Opens a new paper position. Returns the trade record."""
        with _position_lock:
            return self._open_position_locked(
                market_id, question, side, entry_price, amount_usdc,
                estimated_prob, confidence, reasoning,
                simulation_id, report_id, end_date, liquidity, category,
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

        # Conservative fill: buying moves the price against us
        fill_price = _apply_slippage(entry_price, amount_usdc, liquidity, action="buy")
        fee = amount_usdc * FEE_RATE
        net_amount = amount_usdc - fee
        shares = net_amount / fill_price if fill_price > 0 else 0

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
            "current_value": round(net_amount, 4),
            "unrealized_pnl": round(-fee, 4),
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
        })

        self._snapshot_equity()
        logger.info(
            f"Opened paper position: {side} on '{question[:60]}' "
            f"@ {fill_price:.4f} (mid {entry_price:.4f}) for {amount_usdc:.2f} USDC"
        )
        return trade

    def close_position(
        self,
        market_id: str,
        exit_price: float,
        reason: str = "manual",
        liquidity: Optional[float] = None,
    ) -> dict:
        """Closes an open paper position at the given exit price."""
        with _position_lock:
            return self._close_position_locked(market_id, exit_price, reason, liquidity)

    def _close_position_locked(
        self,
        market_id: str,
        exit_price: float,
        reason: str = "manual",
        liquidity: Optional[float] = None,
    ) -> dict:
        """Internal: caller must hold _position_lock."""
        # Re-read from disk inside the lock — another thread may have already
        # closed this position since the caller last checked.
        portfolio = self.db.get_portfolio()

        if market_id not in portfolio["positions"]:
            raise ValueError(f"No open position for market {market_id}")

        pos = portfolio["positions"][market_id]
        close_liquidity = liquidity if liquidity is not None else pos.get("liquidity", 0)
        notional = pos["shares"] * exit_price
        # Conservative fill: selling pushes the price against us
        fill_price = _apply_slippage(exit_price, notional, close_liquidity, action="sell")
        fee = pos["shares"] * fill_price * FEE_RATE
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
        })

        self._snapshot_equity()
        logger.info(
            f"Closed paper position on '{pos['question'][:60]}': "
            f"fill {fill_price:.4f} (mid {exit_price:.4f}) "
            f"PnL = {pnl:+.2f} USDC ({pnl / pos['cost_basis'] * 100:+.1f}%)"
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

    def _snapshot_equity(self):
        stats = self.db.get_stats()
        self.db.record_equity(stats["total_value"])
