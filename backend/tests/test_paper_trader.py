import os
import tempfile
from types import SimpleNamespace

import pytest

from app.services.polymarket import portfolio_db
from app.services.polymarket.paper_trader import FEE_RATE, PaperTrader, _apply_slippage


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    """Each test gets a fresh temporary portfolio directory."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(portfolio_db, "DATA_DIR", tmp)
        yield tmp


@pytest.fixture
def trader():
    return PaperTrader()


def test_open_position_records_trade_and_deducts_balance(trader):
    trade = trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
    )

    portfolio = trader.db.get_portfolio()
    assert portfolio["balance"] == pytest.approx(10_000.0 - 200.0)
    assert "m1" in portfolio["positions"]

    pos = portfolio["positions"]["m1"]
    assert pos["side"] == "NO"
    assert pos["cost_basis"] == 200.0
    assert pos["fee_paid"] == pytest.approx(200.0 * FEE_RATE)
    assert pos["shares"] == pytest.approx((200.0 * (1 - FEE_RATE)) / 0.20)

    assert trade["type"] == "OPEN"
    assert trade["fee"] == pytest.approx(200.0 * FEE_RATE)


def test_close_position_pays_exit_fee(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
    )

    close = trader.close_position("m1", exit_price=0.24, reason="take_profit")
    pos = trader.db.get_portfolio()["positions"]

    assert "m1" not in pos
    assert close["type"] == "CLOSE"
    assert close["fee"] == pytest.approx(close["shares"] * 0.24 * FEE_RATE)
    assert close["pnl"] == pytest.approx(
        close["shares"] * 0.24 * (1 - FEE_RATE) - 200.0
    )


def test_buy_slippage_raises_fill_price():
    fill = _apply_slippage(0.10, amount_usdc=200.0, liquidity=1000.0, action="buy")
    # 200/1000 = 0.2 -> slippage = min(0.05, 0.5*0.2) = 0.05
    assert fill == pytest.approx(0.10 * 1.05)


def test_sell_slippage_lowers_fill_price():
    fill = _apply_slippage(0.20, amount_usdc=200.0, liquidity=10000.0, action="sell")
    # 200/10000 = 0.02 -> slippage = 0.5*0.02 = 0.01
    assert fill == pytest.approx(0.20 * 0.99)


def test_open_with_liquidity_applies_slippage(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.10,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        liquidity=1000.0,
    )
    pos = trader.db.get_portfolio()["positions"]["m1"]
    assert pos["entry_price"] == pytest.approx(0.105)
    assert pos["midpoint_price"] == pytest.approx(0.10)
    assert pos["liquidity"] == 1000.0


def test_close_with_liquidity_applies_slippage(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.10,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        liquidity=1000.0,
    )
    close = trader.close_position("m1", exit_price=0.12, liquidity=1000.0)
    # sell slippage 5% -> fill = 0.114
    assert close["exit_price"] == pytest.approx(0.114)
    assert close["exit_midpoint"] == pytest.approx(0.12)


def test_duplicate_open_raises(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=100.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
    )
    with pytest.raises(ValueError, match="already open"):
        trader.open_position(
            market_id="m1",
            question="Test market?",
            side="YES",
            entry_price=0.80,
            amount_usdc=100.0,
            estimated_prob=0.5,
            confidence="high",
            reasoning="test",
        )


def test_insufficient_balance_raises(trader):
    with pytest.raises(ValueError, match="Insufficient balance"):
        trader.open_position(
            market_id="m1",
            question="Test market?",
            side="NO",
            entry_price=0.20,
            amount_usdc=20_000.0,
            estimated_prob=0.5,
            confidence="high",
            reasoning="test",
        )


def test_category_is_stored(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        category="Politics",
    )
    pos = trader.db.get_portfolio()["positions"]["m1"]
    assert pos["category"] == "Politics"
