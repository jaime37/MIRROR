from datetime import datetime, timedelta, timezone

from app.services.polymarket.backtester import Backtester
from app.services.polymarket.market_fetcher import Market


def make_market(end_days=30, yes_token="token-yes") -> Market:
    end = (datetime.now(timezone.utc) + timedelta(days=end_days)).isoformat()
    return Market(
        id="m1",
        condition_id="c1",
        question="Will the underdog win?",
        description="test",
        end_date=end,
        category="Test",
        volume=100_000.0,
        liquidity=50_000.0,
        yes_token_id=yes_token,
        no_token_id="token-no",
        yes_price=0.85,
        no_price=0.15,
        active=True,
        closed=False,
        enable_order_book=True,
    )


def test_simulate_market_generates_contrarian_trade(monkeypatch):
    """YES spikes to 87%, the strategy buys NO, then YES falls back to 50%."""
    settings = {
        "min_edge": 0.05,
        "position_size_usdc": 200.0,
        "min_entry_price": 0.03,
        "tp_standard": 0.20,
        "sl_standard": -0.15,
        "hard_stop_pp_standard": 0.10,
    }
    bt = Backtester(settings=settings, fidelity_minutes=720)

    # History: YES price series (seconds timestamps)
    history = [
        {"t": 1_700_000_000, "p": 0.50},
        {"t": 1_700_010_000, "p": 0.87},  # enter NO here
        {"t": 1_700_020_000, "p": 0.60},  # TP hit for NO
        {"t": 1_700_030_000, "p": 0.55},
    ]
    monkeypatch.setattr(bt, "fetch_price_history", lambda _tid: history)

    market = make_market()
    trades = bt.simulate_market(market)

    assert len(trades) == 1
    t = trades[0]
    assert t.side == "NO"
    assert t.reason == "take_profit"
    assert t.pnl > 0


def test_no_trade_when_not_extreme(monkeypatch):
    settings = {
        "min_edge": 0.05,
        "position_size_usdc": 200.0,
        "min_entry_price": 0.03,
    }
    bt = Backtester(settings=settings, fidelity_minutes=720)
    history = [
        {"t": 1_700_000_000, "p": 0.55},
        {"t": 1_700_010_000, "p": 0.60},
        {"t": 1_700_020_000, "p": 0.58},
    ]
    monkeypatch.setattr(bt, "fetch_price_history", lambda _tid: history)

    trades = bt.simulate_market(make_market())
    assert len(trades) == 0


def test_run_aggregates_metrics(monkeypatch):
    settings = {
        "min_edge": 0.05,
        "position_size_usdc": 200.0,
        "min_entry_price": 0.03,
        "tp_standard": 0.20,
        "sl_standard": -0.15,
        "hard_stop_pp_standard": 0.10,
    }
    bt = Backtester(settings=settings, fidelity_minutes=720)

    history = [
        {"t": 1_700_000_000, "p": 0.50},
        {"t": 1_700_010_000, "p": 0.87},
        {"t": 1_700_020_000, "p": 0.60},
    ]
    monkeypatch.setattr(bt, "fetch_price_history", lambda _tid: history)

    # Patch market fetcher so we don't hit the network
    fake_market = make_market()
    monkeypatch.setattr(
        "app.services.polymarket.backtester.MarketFetcher.get_active_markets",
        lambda *args, **kwargs: [fake_market],
    )

    results = bt.run(limit=1)
    assert results["markets_tested"] == 1
    assert results["total_trades"] == 1
    assert results["win_rate"] == 1.0
    assert results["total_pnl"] > 0
