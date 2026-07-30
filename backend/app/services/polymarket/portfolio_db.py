"""
Persistence layer for paper trading portfolio state.

Uses SQLite with WAL mode for transactional safety. On first run, existing
JSON files (legacy) are automatically migrated into the database.
"""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(__file__), "../../../uploads/paper_trading")

# Legacy JSON filenames (used for one-time migration)
PORTFOLIO_FILE = "portfolio.json"
TRADES_FILE = "trades.json"
EQUITY_FILE = "equity_history.json"


def _db_file() -> str:
    # Resolved at call time so tests can monkeypatch DATA_DIR
    return os.path.join(DATA_DIR, "portfolio.db")


class PortfolioDatabase:
    INITIAL_BALANCE = 10_000.0  # starting virtual USDC

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        json_portfolio = os.path.join(DATA_DIR, PORTFOLIO_FILE)
        needs_migration = not os.path.exists(self._db_path()) and os.path.exists(json_portfolio)
        self._init_db()
        if needs_migration:
            self._migrate_json_if_needed()

    # ── Connection ──────────────────────────────────────────────────────────────

    def _db_path(self) -> str:
        return _db_file()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path(), timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Schema ──────────────────────────────────────────────────────────────────

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS portfolio (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance REAL NOT NULL,
                    initial_balance REAL NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    market_id TEXT PRIMARY KEY,
                    question TEXT,
                    side TEXT,
                    entry_price REAL,
                    midpoint_price REAL,
                    category TEXT,
                    shares REAL,
                    cost_basis REAL,
                    fee_paid REAL,
                    liquidity REAL,
                    current_price REAL,
                    current_value REAL,
                    unrealized_pnl REAL,
                    estimated_prob REAL,
                    confidence TEXT,
                    reasoning TEXT,
                    simulation_id TEXT,
                    report_id TEXT,
                    opened_at TEXT,
                    end_date TEXT,
                    status TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    pk INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL,
                    type TEXT,
                    market_id TEXT,
                    question TEXT,
                    side TEXT,
                    price REAL,
                    midpoint_price REAL,
                    entry_price REAL,
                    exit_price REAL,
                    exit_midpoint REAL,
                    shares REAL,
                    amount_usdc REAL,
                    proceeds REAL,
                    fee REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    estimated_prob REAL,
                    confidence TEXT,
                    reasoning TEXT,
                    category TEXT,
                    simulation_id TEXT,
                    report_id TEXT,
                    reason TEXT,
                    opened_at TEXT,
                    settings_version TEXT,
                    entry_bucket TEXT,
                    execution_mode TEXT,
                    timestamp TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_trades_market_id ON trades(market_id);
                CREATE INDEX IF NOT EXISTS idx_trades_type ON trades(type);

                CREATE TABLE IF NOT EXISTS equity_history (
                    timestamp TEXT PRIMARY KEY,
                    value REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prob_estimates (
                    pk INTEGER PRIMARY KEY AUTOINCREMENT,
                    estimate_id TEXT,
                    market_id TEXT,
                    question TEXT,
                    side TEXT,
                    estimated_prob REAL,
                    confidence TEXT,
                    edge REAL,
                    market_price REAL,
                    as_of TEXT,
                    model TEXT,
                    prompt_version TEXT,
                    settings_version TEXT,
                    outcome REAL,
                    brier REAL,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS position_marks (
                    pk INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT,
                    ts TEXT,
                    price REAL
                );

                CREATE INDEX IF NOT EXISTS idx_position_marks_market_id ON position_marks(market_id);
                """
            )
            # Idempotent migrations for databases created before these columns existed
            for stmt in (
                "ALTER TABLE trades ADD COLUMN settings_version TEXT",
                "ALTER TABLE trades ADD COLUMN entry_bucket TEXT",
                "ALTER TABLE trades ADD COLUMN execution_mode TEXT",
            ):
                try:
                    conn.execute(stmt)
                except Exception:
                    pass  # column already exists

    # ── JSON migration (one-time) ───────────────────────────────────────────────

    def _migrate_json_if_needed(self):
        """Import legacy JSON files into the newly-created SQLite database."""
        portfolio_path = os.path.join(DATA_DIR, PORTFOLIO_FILE)
        trades_path = os.path.join(DATA_DIR, TRADES_FILE)
        equity_path = os.path.join(DATA_DIR, EQUITY_FILE)

        if not os.path.exists(portfolio_path):
            return

        try:
            with open(portfolio_path, "r", encoding="utf-8") as f:
                portfolio = json.load(f)
        except Exception:
            return

        self.save_portfolio(portfolio)

        if os.path.exists(trades_path):
            try:
                with open(trades_path, "r", encoding="utf-8") as f:
                    trades = json.load(f)
                for trade in trades:
                    self.add_trade(trade)
            except Exception:
                pass

        if os.path.exists(equity_path):
            try:
                with open(equity_path, "r", encoding="utf-8") as f:
                    equity = json.load(f)
                for row in equity:
                    self.record_equity(row.get("value", 0), timestamp=row.get("timestamp"))
            except Exception:
                pass

    # ── Portfolio ───────────────────────────────────────────────────────────────

    def get_portfolio(self) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT balance, initial_balance, created_at FROM portfolio WHERE id = 1"
            ).fetchone()
            if row is None:
                return {
                    "balance": self.INITIAL_BALANCE,
                    "initial_balance": self.INITIAL_BALANCE,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "positions": {},
                }

            positions = {}
            for pos in conn.execute("SELECT * FROM positions"):
                positions[pos["market_id"]] = dict(pos)

            return {
                "balance": row["balance"],
                "initial_balance": row["initial_balance"],
                "created_at": row["created_at"],
                "positions": positions,
            }

    def save_portfolio(self, portfolio: dict):
        with self._conn() as conn:
            conn.execute(
                """
                REPLACE INTO portfolio (id, balance, initial_balance, created_at)
                VALUES (1, ?, ?, ?)
                """,
                (
                    portfolio.get("balance", self.INITIAL_BALANCE),
                    portfolio.get("initial_balance", self.INITIAL_BALANCE),
                    portfolio.get("created_at", datetime.now(timezone.utc).isoformat()),
                ),
            )
            conn.execute("DELETE FROM positions")
            for market_id, pos in portfolio.get("positions", {}).items():
                conn.execute(
                    """
                    INSERT INTO positions (
                        market_id, question, side, entry_price, midpoint_price, category,
                        shares, cost_basis, fee_paid, liquidity, current_price, current_value,
                        unrealized_pnl, estimated_prob, confidence, reasoning, simulation_id,
                        report_id, opened_at, end_date, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        market_id,
                        pos.get("question", ""),
                        pos.get("side", ""),
                        pos.get("entry_price", 0),
                        pos.get("midpoint_price", 0),
                        pos.get("category", ""),
                        pos.get("shares", 0),
                        pos.get("cost_basis", 0),
                        pos.get("fee_paid", 0),
                        pos.get("liquidity", 0),
                        pos.get("current_price", 0),
                        pos.get("current_value", 0),
                        pos.get("unrealized_pnl", 0),
                        pos.get("estimated_prob", 0),
                        pos.get("confidence", ""),
                        pos.get("reasoning", ""),
                        pos.get("simulation_id", ""),
                        pos.get("report_id", ""),
                        pos.get("opened_at", ""),
                        pos.get("end_date", ""),
                        pos.get("status", ""),
                    ),
                )

    def reset_portfolio(self):
        now = datetime.now(timezone.utc).isoformat()
        portfolio = {
            "balance": self.INITIAL_BALANCE,
            "initial_balance": self.INITIAL_BALANCE,
            "created_at": now,
            "positions": {},
        }
        self.save_portfolio(portfolio)
        with self._conn() as conn:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM equity_history")

    # ── Trades ──────────────────────────────────────────────────────────────────

    def get_trades(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY pk"
            ).fetchall()
            return [self._row_to_trade(row) for row in rows]

    def add_trade(self, trade: dict) -> dict:
        trade = dict(trade)
        if "id" not in trade or not trade["id"]:
            trade["id"] = str(uuid.uuid4())[:8]
        if "timestamp" not in trade or not trade["timestamp"]:
            trade["timestamp"] = datetime.now(timezone.utc).isoformat()

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO trades (
                    trade_id, type, market_id, question, side, price, midpoint_price,
                    entry_price, exit_price, exit_midpoint, shares, amount_usdc, proceeds,
                    fee, pnl, pnl_pct, estimated_prob, confidence, reasoning, category,
                    simulation_id, report_id, reason, opened_at,
                    settings_version, entry_bucket, execution_mode, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.get("id", ""),
                    trade.get("type", ""),
                    trade.get("market_id", ""),
                    trade.get("question", ""),
                    trade.get("side", ""),
                    trade.get("price", 0),
                    trade.get("midpoint_price", 0),
                    trade.get("entry_price", 0),
                    trade.get("exit_price", 0),
                    trade.get("exit_midpoint", 0),
                    trade.get("shares", 0),
                    trade.get("amount_usdc", 0),
                    trade.get("proceeds", 0),
                    trade.get("fee", 0),
                    trade.get("pnl", 0),
                    trade.get("pnl_pct", 0),
                    trade.get("estimated_prob", 0),
                    trade.get("confidence", ""),
                    trade.get("reasoning", ""),
                    trade.get("category", ""),
                    trade.get("simulation_id", ""),
                    trade.get("report_id", ""),
                    trade.get("reason", ""),
                    trade.get("opened_at", ""),
                    trade.get("settings_version", ""),
                    trade.get("entry_bucket", ""),
                    trade.get("execution_mode", ""),
                    trade.get("timestamp", ""),
                ),
            )
        return trade

    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> dict:
        return {
            "id": row["trade_id"],
            "type": row["type"],
            "market_id": row["market_id"],
            "question": row["question"],
            "side": row["side"],
            "price": row["price"],
            "midpoint_price": row["midpoint_price"],
            "entry_price": row["entry_price"],
            "exit_price": row["exit_price"],
            "exit_midpoint": row["exit_midpoint"],
            "shares": row["shares"],
            "amount_usdc": row["amount_usdc"],
            "proceeds": row["proceeds"],
            "fee": row["fee"],
            "pnl": row["pnl"],
            "pnl_pct": row["pnl_pct"],
            "estimated_prob": row["estimated_prob"],
            "confidence": row["confidence"],
            "reasoning": row["reasoning"],
            "category": row["category"],
            "simulation_id": row["simulation_id"],
            "report_id": row["report_id"],
            "reason": row["reason"],
            "opened_at": row["opened_at"],
            "settings_version": row["settings_version"],
            "entry_bucket": row["entry_bucket"],
            "execution_mode": row["execution_mode"],
            "timestamp": row["timestamp"],
        }

    # ── Probability estimates (calibration) ─────────────────────────────────────

    def add_prob_estimate(self, d: dict) -> dict:
        d = dict(d)
        if "estimate_id" not in d or not d["estimate_id"]:
            d["estimate_id"] = str(uuid.uuid4())[:8]
        if "created_at" not in d or not d["created_at"]:
            d["created_at"] = datetime.now(timezone.utc).isoformat()

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO prob_estimates (
                    estimate_id, market_id, question, side, estimated_prob, confidence,
                    edge, market_price, as_of, model, prompt_version, settings_version,
                    outcome, brier, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d.get("estimate_id", ""),
                    d.get("market_id", ""),
                    d.get("question", ""),
                    d.get("side", ""),
                    d.get("estimated_prob", 0),
                    d.get("confidence", ""),
                    d.get("edge", 0),
                    d.get("market_price", 0),
                    d.get("as_of", ""),
                    d.get("model", ""),
                    d.get("prompt_version", ""),
                    d.get("settings_version", ""),
                    d.get("outcome"),
                    d.get("brier"),
                    d.get("created_at", ""),
                ),
            )
        return d

    def get_prob_estimates(self, market_id: Optional[str] = None) -> list:
        with self._conn() as conn:
            if market_id is not None:
                rows = conn.execute(
                    "SELECT * FROM prob_estimates WHERE market_id = ? ORDER BY pk",
                    (market_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM prob_estimates ORDER BY pk").fetchall()
            return [dict(r) for r in rows]

    def resolve_prob_estimates(self, market_id: str, outcome: float) -> int:
        """
        Marks all unresolved estimates for a market with the outcome FOR THE
        POSITION'S SIDE (1.0 if the side won, 0.0 otherwise) and computes
        brier = (estimated_prob - outcome)^2. Returns rows resolved.
        """
        resolved = 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT pk, estimated_prob FROM prob_estimates WHERE market_id = ? AND outcome IS NULL",
                (market_id,),
            ).fetchall()
            for row in rows:
                brier = (row["estimated_prob"] - outcome) ** 2
                conn.execute(
                    "UPDATE prob_estimates SET outcome = ?, brier = ? WHERE pk = ?",
                    (outcome, brier, row["pk"]),
                )
                resolved += 1
        return resolved

    # ── Position marks (observability) ──────────────────────────────────────────

    def add_position_mark(self, market_id: str, price: float, ts: Optional[str] = None):
        if ts is None:
            ts = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO position_marks (market_id, ts, price) VALUES (?, ?, ?)",
                (market_id, ts, round(price, 4)),
            )

    def get_position_marks(self, market_id: str) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT market_id, ts, price FROM position_marks WHERE market_id = ? ORDER BY pk",
                (market_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Equity history ──────────────────────────────────────────────────────────

    def get_equity_history(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT timestamp, value FROM equity_history ORDER BY timestamp"
            ).fetchall()
            return [{"timestamp": r["timestamp"], "value": r["value"]} for r in rows]

    def record_equity(self, total_value: float, timestamp: Optional[str] = None):
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity_history (timestamp, value) VALUES (?, ?)",
                (timestamp, round(total_value, 4)),
            )
            # keep last 2000 snapshots
            conn.execute(
                """
                DELETE FROM equity_history
                WHERE timestamp NOT IN (
                    SELECT timestamp FROM equity_history ORDER BY timestamp DESC LIMIT 2000
                )
                """
            )

    # ── Stats ───────────────────────────────────────────────────────────────────

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

        positions_value = sum(
            pos.get("current_value", pos.get("cost_basis", 0))
            for pos in portfolio.get("positions", {}).values()
        )
        total_value = current_balance + positions_value

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
