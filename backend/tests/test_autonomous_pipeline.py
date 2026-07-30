import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

# The pipeline imports the LLM client which validates the API key on config load.
os.environ.setdefault("LLM_API_KEY", "dummy-test-key")

from app.services.polymarket.autonomous_pipeline import (
    SETTINGS_VERSION,
    AutonomousPipeline,
    entry_bucket,
    load_settings,
)


@pytest.fixture
def pipeline():
    return AutonomousPipeline()


def test_load_settings_contains_new_keys():
    settings = load_settings()
    assert "min_opportunity_score" in settings
    assert "max_positions_per_category" in settings
    assert "dd_soft_reduce_pct" in settings
    assert settings["max_drawdown_pause_pct"] == pytest.approx(0.15)


def test_days_to_expiry(pipeline):
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert pipeline._days_to_expiry(future) == pytest.approx(30.0, rel=0.01)


def test_days_to_expiry_returns_none_for_empty_date(pipeline):
    assert pipeline._days_to_expiry("") is None


def test_score_opportunity_rewards_high_edge_and_liquidity(pipeline):
    market = SimpleNamespace(
        yes_price=0.85,
        no_price=0.15,
        liquidity=50_000.0,
        end_date=(datetime.now(timezone.utc) + timedelta(days=45)).isoformat(),
        category="Politics",
    )
    score = pipeline._score_opportunity(market, edge=0.15, news_summary=["a"] * 5, open_categories={})
    assert score >= 70.0


def test_score_opportunity_penalizes_low_liquidity_and_short_expiry(pipeline):
    market = SimpleNamespace(
        yes_price=0.85,
        no_price=0.15,
        liquidity=1_000.0,
        end_date=(datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        category="Politics",
    )
    score = pipeline._score_opportunity(market, edge=0.05, news_summary=["a"], open_categories={})
    assert score < 40.0


def test_score_opportunity_penalizes_category_concentration(pipeline):
    market = SimpleNamespace(
        yes_price=0.85,
        no_price=0.15,
        liquidity=50_000.0,
        end_date=(datetime.now(timezone.utc) + timedelta(days=45)).isoformat(),
        category="Politics",
    )
    concentrated = {"Politics": 2}
    open_score = pipeline._score_opportunity(market, edge=0.15, news_summary=["a"] * 5, open_categories=concentrated)
    clean_score = pipeline._score_opportunity(market, edge=0.15, news_summary=["a"] * 5, open_categories={})
    assert open_score < clean_score


def test_score_opportunity_capped_between_0_and_100(pipeline):
    market = SimpleNamespace(
        yes_price=0.85,
        no_price=0.15,
        liquidity=1_000_000.0,
        end_date=(datetime.now(timezone.utc) + timedelta(days=200)).isoformat(),
        category="Politics",
    )
    score = pipeline._score_opportunity(market, edge=0.50, news_summary=["a"] * 10, open_categories={})
    assert 0.0 <= score <= 100.0


def test_score_opportunity_geopolitics_fee_free_bonus(pipeline):
    base = dict(
        yes_price=0.85,
        no_price=0.15,
        liquidity=50_000.0,
        end_date=(datetime.now(timezone.utc) + timedelta(days=45)).isoformat(),
    )
    geo = pipeline._score_opportunity(
        SimpleNamespace(**base, category="Geopolitics"), edge=0.15, news_summary=["a"] * 5, open_categories={}
    )
    other = pipeline._score_opportunity(
        SimpleNamespace(**base, category="Politics"), edge=0.15, news_summary=["a"] * 5, open_categories={}
    )
    assert geo == pytest.approx(min(100.0, other + 10.0))


def test_stale_close_disabled_by_default(monkeypatch, tmp_path):
    # Patch the settings file away so we test pure code defaults + HARDCODE
    import app.services.polymarket.autonomous_pipeline as ap
    monkeypatch.setattr(ap, "SETTINGS_FILE", str(tmp_path / "missing_settings.json"))
    settings = load_settings()
    assert settings["stale_position_days"] == 0


def test_new_exit_settings_defaults(monkeypatch, tmp_path):
    import app.services.polymarket.autonomous_pipeline as ap
    monkeypatch.setattr(ap, "SETTINGS_FILE", str(tmp_path / "missing_settings.json"))
    settings = load_settings()
    assert settings["sl_under_05"] == pytest.approx(0.0)
    assert settings["sl_under_10"] == pytest.approx(-0.40)
    assert settings["sl_under_20"] == pytest.approx(-0.25)
    assert settings["sl_standard"] == pytest.approx(-0.15)
    assert settings["catastrophic_stop_pct"] == pytest.approx(0.50)
    assert settings["max_position_age_days"] == 60
    assert settings["execution_mode"] == "maker"
    assert SETTINGS_VERSION


def test_entry_bucket():
    assert entry_bucket(0.02) == "0-0.05"
    assert entry_bucket(0.07) == "0.05-0.10"
    assert entry_bucket(0.15) == "0.10-0.20"
    assert entry_bucket(0.25) == "0.20-0.30"
    assert entry_bucket(0.50) == "0.30-0.70"
    assert entry_bucket(0.75) == "0.70-0.80"
    assert entry_bucket(0.85) == "0.80-0.90"
    assert entry_bucket(0.93) == "0.90-0.95"
    assert entry_bucket(0.98) == "0.95-1"
