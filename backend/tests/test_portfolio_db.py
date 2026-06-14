import json
import os
import tempfile

import pytest

from app.services.polymarket import portfolio_db
from app.services.polymarket.portfolio_db import PortfolioDatabase


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    """Each test gets a fresh temporary portfolio directory."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(portfolio_db, "DATA_DIR", tmp)
        yield tmp


def test_default_portfolio():
    db = PortfolioDatabase()
    portfolio = db.get_portfolio()
    assert portfolio["balance"] == PortfolioDatabase.INITIAL_BALANCE
    assert portfolio["initial_balance"] == PortfolioDatabase.INITIAL_BALANCE
    assert portfolio["positions"] == {}


def test_save_and_reload_portfolio():
    db = PortfolioDatabase()
    portfolio = {
        "balance": 9500.0,
        "initial_balance": 10_000.0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "positions": {
            "m1": {
                "market_id": "m1",
                "question": "Q?",
                "side": "NO",
                "entry_price": 0.20,
                "midpoint_price": 0.19,
                "category": "Politics",
                "shares": 1000.0,
                "cost_basis": 200.0,
                "fee_paid": 4.0,
                "liquidity": 50_000.0,
                "current_price": 0.20,
                "current_value": 200.0,
                "unrealized_pnl": 0.0,
                "estimated_prob": 0.5,
                "confidence": "high",
                "reasoning": "test",
                "simulation_id": "",
                "report_id": "",
                "opened_at": "2026-01-01T00:00:00+00:00",
                "end_date": "",
                "status": "open",
            }
        },
    }
    db.save_portfolio(portfolio)

    reloaded = db.get_portfolio()
    assert reloaded["balance"] == pytest.approx(9500.0)
    assert "m1" in reloaded["positions"]
    assert reloaded["positions"]["m1"]["category"] == "Politics"


def test_add_trade():
    db = PortfolioDatabase()
    trade = db.add_trade({
        "type": "OPEN",
        "market_id": "m1",
        "question": "Q?",
        "side": "NO",
        "price": 0.20,
        "shares": 1000.0,
        "amount_usdc": 200.0,
        "fee": 4.0,
        "confidence": "high",
        "reasoning": "test",
    })
    assert trade["id"]
    assert trade["timestamp"]

    trades = db.get_trades()
    assert len(trades) == 1
    assert trades[0]["market_id"] == "m1"


def test_record_and_trim_equity():
    db = PortfolioDatabase()
    for i in range(5):
        db.record_equity(10_000.0 + i, timestamp=f"2026-01-0{i+1}T00:00:00+00:00")

    history = db.get_equity_history()
    assert len(history) == 5
    assert history[-1]["value"] == pytest.approx(10_004.0)


def test_stats():
    db = PortfolioDatabase()
    db.save_portfolio({
        "balance": 9800.0,
        "initial_balance": 10_000.0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "positions": {},
    })
    db.add_trade({
        "type": "CLOSE",
        "market_id": "m1",
        "question": "Q?",
        "side": "NO",
        "exit_price": 0.24,
        "shares": 1000.0,
        "proceeds": 235.2,
        "fee": 4.8,
        "pnl": 35.2,
        "pnl_pct": 17.6,
        "reason": "take_profit",
    })
    stats = db.get_stats()
    assert stats["total_pnl"] == pytest.approx(35.2)
    assert stats["win_rate"] == 100.0
    assert stats["wins"] == 1


def test_json_migration(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(portfolio_db, "DATA_DIR", tmp)

        # Write legacy JSON files
        portfolio = {
            "balance": 9000.0,
            "initial_balance": 10_000.0,
            "created_at": "2026-01-01T00:00:00+00:00",
            "positions": {
                "m1": {
                    "market_id": "m1",
                    "question": "Q?",
                    "side": "NO",
                    "entry_price": 0.20,
                    "shares": 1000.0,
                    "cost_basis": 200.0,
                    "fee_paid": 4.0,
                    "current_price": 0.20,
                    "current_value": 200.0,
                    "unrealized_pnl": 0.0,
                    "estimated_prob": 0.5,
                    "confidence": "high",
                    "reasoning": "test",
                    "opened_at": "2026-01-01T00:00:00+00:00",
                    "end_date": "",
                    "status": "open",
                }
            },
        }
        with open(os.path.join(tmp, "portfolio.json"), "w", encoding="utf-8") as f:
            json.dump(portfolio, f)
        with open(os.path.join(tmp, "trades.json"), "w", encoding="utf-8") as f:
            json.dump([{"type": "OPEN", "market_id": "m1"}], f)
        with open(os.path.join(tmp, "equity_history.json"), "w", encoding="utf-8") as f:
            json.dump([{"timestamp": "2026-01-01T00:00:00+00:00", "value": 10_000.0}], f)

        db = PortfolioDatabase()
        reloaded = db.get_portfolio()
        assert reloaded["balance"] == pytest.approx(9000.0)
        assert "m1" in reloaded["positions"]
        assert len(db.get_trades()) == 1
        assert len(db.get_equity_history()) == 1
