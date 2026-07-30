import os
import tempfile
from types import SimpleNamespace

import pytest

from app.services.polymarket import portfolio_db
from app.services.polymarket.paper_trader import (
    FEE_RATE,
    PaperTrader,
    _apply_slippage,
    _walk_book,
    fee_rate_for_category,
    taker_fee,
)


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
    # Real taker fee: shares*fill + fee == amount exactly (unknown category → 5%)
    expected_shares = 200.0 / (0.20 * (1 + 0.05 * (1 - 0.20)))
    expected_fee = taker_fee(expected_shares, 0.20, "")
    assert pos["shares"] == pytest.approx(expected_shares, abs=1e-4)
    assert pos["fee_paid"] == pytest.approx(expected_fee, abs=1e-4)

    assert trade["type"] == "OPEN"
    assert trade["fee"] == pytest.approx(expected_fee, abs=1e-4)


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
    expected_fee = taker_fee(close["shares"], 0.24, "")
    assert close["fee"] == pytest.approx(expected_fee, abs=1e-4)
    assert close["pnl"] == pytest.approx(
        close["shares"] * 0.24 - expected_fee - 200.0, abs=1e-3
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


# ── Real fee model ────────────────────────────────────────────────────────────

def test_fee_rate_for_category():
    assert fee_rate_for_category("Politics") == pytest.approx(0.04)
    assert fee_rate_for_category("US POLITICS") == pytest.approx(0.04)
    assert fee_rate_for_category("Finance") == pytest.approx(0.04)
    assert fee_rate_for_category("Tech") == pytest.approx(0.04)
    assert fee_rate_for_category("Crypto") == pytest.approx(0.07)
    assert fee_rate_for_category("Geopolitics") == pytest.approx(0.0)
    assert fee_rate_for_category("Sports") == pytest.approx(0.05)
    assert fee_rate_for_category("") == pytest.approx(0.05)
    assert fee_rate_for_category(None) == pytest.approx(0.05)


def test_taker_fee_formula():
    # shares × rate × price × (1 − price)
    assert taker_fee(1000.0, 0.20, "Politics") == pytest.approx(1000.0 * 0.04 * 0.20 * 0.80)
    assert taker_fee(1000.0, 0.20, "Geopolitics") == pytest.approx(0.0)


def test_legacy_fee_rate_still_exported():
    assert FEE_RATE == pytest.approx(0.02)


def test_geopolitics_open_pays_zero_fee(trader):
    trade = trader.open_position(
        market_id="m1",
        question="Geopolitics market?",
        side="YES",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        category="Geopolitics",
    )
    assert trade["fee"] == pytest.approx(0.0)
    pos = trader.db.get_portfolio()["positions"]["m1"]
    assert pos["shares"] == pytest.approx(200.0 / 0.20)


# ── Execution modes / order book ─────────────────────────────────────────────

def test_walk_book_vwap():
    asks = [(0.20, 100.0), (0.22, 100.0), (0.25, 500.0)]
    vwap, filled = _walk_book(asks, 200.0)
    assert filled == pytest.approx(200.0)
    assert vwap == pytest.approx((100 * 0.20 + 100 * 0.22) / 200)


def test_walk_book_shallow_book_prices_remainder_at_last_level():
    asks = [(0.20, 50.0)]
    vwap, filled = _walk_book(asks, 200.0)
    assert filled == pytest.approx(200.0)  # remainder priced at last level
    assert vwap == pytest.approx(0.20)


def test_maker_open_fills_at_best_bid_with_zero_fee(trader):
    book = {"bids": [(0.19, 500.0), (0.18, 500.0)], "asks": [(0.21, 500.0)]}
    trade = trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=190.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        execution_mode="maker",
        book=book,
    )
    assert trade["price"] == pytest.approx(0.19)  # best bid
    assert trade["fee"] == pytest.approx(0.0)
    assert trade["execution_mode"] == "maker"
    assert trade["shares"] == pytest.approx(190.0 / 0.19)


def test_maker_open_without_book_fills_at_mid(trader):
    trade = trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        execution_mode="maker",
    )
    assert trade["price"] == pytest.approx(0.20)
    assert trade["fee"] == pytest.approx(0.0)


def test_taker_open_walks_asks(trader):
    book = {"bids": [(0.19, 500.0)], "asks": [(0.20, 100.0), (0.22, 100.0)]}
    trade = trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=40.0,  # ~200 shares → consumes both ask levels
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        execution_mode="taker",
        book=book,
    )
    assert trade["price"] == pytest.approx(0.21)  # VWAP of the two levels


def test_maker_close_fills_at_best_ask_with_zero_fee(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        execution_mode="maker",
    )
    book = {"bids": [(0.23, 500.0)], "asks": [(0.25, 500.0)]}
    close = trader.close_position(
        "m1", exit_price=0.24, reason="take_profit", execution_mode="maker", book=book
    )
    assert close["exit_price"] == pytest.approx(0.25)  # best ask
    assert close["fee"] == pytest.approx(0.0)
    assert close["execution_mode"] == "maker"


def test_taker_close_walks_bids(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        execution_mode="maker",
    )
    shares = 200.0 / 0.20  # maker fill at mid
    book = {"bids": [(0.24, shares / 2), (0.22, shares / 2)], "asks": [(0.26, 500.0)]}
    close = trader.close_position(
        "m1", exit_price=0.24, reason="take_profit", execution_mode="taker", book=book
    )
    assert close["exit_price"] == pytest.approx((0.24 + 0.22) / 2)  # VWAP


# ── Binary payout ─────────────────────────────────────────────────────────────

def test_binary_payout_close_winner(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="YES",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        execution_mode="maker",
    )
    shares = 200.0 / 0.20
    close = trader.close_position("m1", exit_price=0.99, reason="expired", payout=1.0)
    assert close["fee"] == pytest.approx(0.0)
    assert close["proceeds"] == pytest.approx(shares * 1.0)
    assert close["pnl"] == pytest.approx(shares - 200.0)


def test_binary_payout_close_loser(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="YES",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        execution_mode="maker",
    )
    close = trader.close_position("m1", exit_price=0.01, reason="expired", payout=0.0)
    assert close["fee"] == pytest.approx(0.0)
    assert close["proceeds"] == pytest.approx(0.0)
    assert close["pnl"] == pytest.approx(-200.0)


# ── Position marks ────────────────────────────────────────────────────────────

def test_update_position_price_records_mark(trader):
    trader.open_position(
        market_id="m1",
        question="Test market?",
        side="NO",
        entry_price=0.20,
        amount_usdc=200.0,
        estimated_prob=0.5,
        confidence="high",
        reasoning="test",
        execution_mode="maker",
    )
    trader.update_position_price("m1", 0.25)
    trader.update_position_price("m1", 0.27)
    marks = trader.db.get_position_marks("m1")
    assert len(marks) == 2
    assert marks[0]["price"] == pytest.approx(0.25)
    assert marks[1]["price"] == pytest.approx(0.27)
