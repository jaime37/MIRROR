"""
JSON-based persistence layer for paper trading portfolio state.
Stores trades, positions, and equity snapshots in uploads/paper_trading/.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(__file__), "../../../uploads/paper_trading")


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _path(filename: str) -> str:
    _ensure_dir()
    return os.path.join(DATA_DIR, filename)


def _load(filename: str, default) -> any:
    p = _path(filename)
    if not os.path.exists(p):
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(filename: str, data: any):
    with open(_path(filename), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class PortfolioDatabase:
    PORTFOLIO_FILE = "portfolio.json"
    TRADES_FILE = "trades.json"
    EQUITY_FILE = "equity_history.json"

    INITIAL_BALANCE = 10_000.0  # starting virtual USDC

    # ---------- Portfolio ----------

    def get_portfolio(self) -> dict:
        default = {
            "balance": self.INITIAL_BALANCE,
            "initial_balance": self.INITIAL_BALANCE,
            "positions": {},  # market_id -> position dict
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return _load(self.PORTFOLIO_FILE, default)

    def save_portfolio(self, portfolio: dict):
        _save(self.PORTFOLIO_FILE, portfolio)

    def reset_portfolio(self):
        portfolio = {
            "balance": self.INITIAL_BALANCE,
            "initial_balance": self.INITIAL_BALANCE,
            "positions": {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _save(self.PORTFOLIO_FILE, portfolio)
        _save(self.TRADES_FILE, [])
        _save(self.EQUITY_FILE, [])

    # ---------- Trades ----------

    def get_trades(self) -> list:
        return _load(self.TRADES_FILE, [])

    def add_trade(self, trade: dict) -> dict:
        trade["id"] = str(uuid.uuid4())[:8]
        trade["timestamp"] = datetime.now(timezone.utc).isoformat()
        trades = self.get_trades()
        trades.append(trade)
        _save(self.TRADES_FILE, trades)
        return trade

    # ---------- Equity history ----------

    def get_equity_history(self) -> list:
        return _load(self.EQUITY_FILE, [])

    def record_equity(self, total_value: float):
        history = self.get_equity_history()
        history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "value": round(total_value, 4),
        })
        # keep last 2000 snapshots
        if len(history) > 2000:
            history = history[-2000:]
        _save(self.EQUITY_FILE, history)

    # ---------- Stats ----------

    def get_stats(self) -> dict:
        trades = self.get_trades()
        portfolio = self.get_portfolio()

        # Deduplicate CLOSE records: a race condition bug (pre-fix commit 408a528) could
        # write multiple CLOSE records for the same position. For stats purposes, keep
        # only the FIRST close per market_id (earliest timestamp = the real close).
        seen_market_ids: dict = {}
        for t in trades:
            if t.get("type") == "CLOSE":
                mid = t.get("market_id", "")
                ts = t.get("timestamp", "")
                if mid not in seen_market_ids or ts < seen_market_ids[mid]["timestamp"]:
                    seen_market_ids[mid] = t
        closed = list(seen_market_ids.values())

        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]
        realized_pnl = sum(t.get("pnl", 0) for t in closed)
        win_rate = len(wins) / len(closed) if closed else 0.0

        equity_history = self.get_equity_history()
        initial = portfolio.get("initial_balance", self.INITIAL_BALANCE)
        current_balance = portfolio.get("balance", initial)

        # estimate open positions value
        positions_value = sum(
            pos.get("current_value", pos.get("cost_basis", 0))
            for pos in portfolio.get("positions", {}).values()
        )
        total_value = current_balance + positions_value

        # unrealized PnL from open positions (so total_pnl reflects true profit)
        unrealized_pnl = sum(
            pos.get("unrealized_pnl", 0)
            for pos in portfolio.get("positions", {}).values()
        )
        total_pnl = realized_pnl + unrealized_pnl

        return {
            "balance": round(current_balance, 4),
            "positions_value": round(positions_value, 4),
            "total_value": round(total_value, 4),
            "total_pnl": round(total_pnl, 4),
            "realized_pnl": round(realized_pnl, 4),
            "unrealized_pnl": round(unrealized_pnl, 4),
            "total_return_pct": round((total_value - initial) / initial * 100, 2),
            "total_trades": len(trades),
            "closed_trades": len(closed),
            "open_positions": len(portfolio.get("positions", {})),
            "win_rate": round(win_rate * 100, 1),
            "wins": len(wins),
            "losses": len(losses),
        }
