"""
Paper trading engine. Simulates order execution at real Polymarket prices
without spending real money.
"""

from datetime import datetime, timezone
from typing import Optional
from .portfolio_db import PortfolioDatabase
from ...utils.logger import get_logger

logger = get_logger("mirofish.polymarket.paper_trader")

FEE_RATE = 0.02  # 2% simulated fee per trade


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
    ) -> dict:
        """Opens a new paper position. Returns the trade record."""
        portfolio = self.db.get_portfolio()

        if amount_usdc > portfolio["balance"]:
            raise ValueError(
                f"Insufficient balance: {portfolio['balance']:.2f} USDC available, "
                f"{amount_usdc:.2f} requested"
            )

        if market_id in portfolio["positions"]:
            raise ValueError(f"Position already open for market {market_id}")

        fee = amount_usdc * FEE_RATE
        net_amount = amount_usdc - fee
        shares = net_amount / entry_price if entry_price > 0 else 0

        position = {
            "market_id": market_id,
            "question": question,
            "side": side,
            "entry_price": round(entry_price, 4),
            "shares": round(shares, 4),
            "cost_basis": round(amount_usdc, 4),
            "fee_paid": round(fee, 4),
            "current_price": round(entry_price, 4),
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
            "price": round(entry_price, 4),
            "shares": round(shares, 4),
            "amount_usdc": round(amount_usdc, 4),
            "fee": round(fee, 4),
            "estimated_prob": round(estimated_prob, 4),
            "confidence": confidence,
            "reasoning": reasoning,
            "simulation_id": simulation_id,
            "report_id": report_id,
        })

        self._snapshot_equity()
        logger.info(
            f"Opened paper position: {side} on '{question[:60]}' "
            f"@ {entry_price:.4f} for {amount_usdc:.2f} USDC"
        )
        return trade

    def close_position(
        self,
        market_id: str,
        exit_price: float,
        reason: str = "manual",
    ) -> dict:
        """Closes an open paper position at the given exit price."""
        portfolio = self.db.get_portfolio()

        if market_id not in portfolio["positions"]:
            raise ValueError(f"No open position for market {market_id}")

        pos = portfolio["positions"][market_id]
        fee = pos["shares"] * exit_price * FEE_RATE
        proceeds = pos["shares"] * exit_price - fee
        pnl = proceeds - pos["cost_basis"]

        trade = self.db.add_trade({
            "type": "CLOSE",
            "market_id": market_id,
            "question": pos["question"],
            "side": pos["side"],
            "entry_price": pos["entry_price"],
            "exit_price": round(exit_price, 4),
            "shares": pos["shares"],
            "proceeds": round(proceeds, 4),
            "fee": round(fee, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / pos["cost_basis"] * 100, 2),
            "reason": reason,
            "opened_at": pos["opened_at"],
        })

        portfolio["balance"] = round(portfolio["balance"] + proceeds, 4)
        del portfolio["positions"][market_id]
        self.db.save_portfolio(portfolio)

        self._snapshot_equity()
        logger.info(
            f"Closed paper position on '{pos['question'][:60]}': "
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
