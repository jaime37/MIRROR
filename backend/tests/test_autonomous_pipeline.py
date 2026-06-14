import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

# The pipeline imports the LLM client which validates the API key on config load.
os.environ.setdefault("LLM_API_KEY", "dummy-test-key")

from app.services.polymarket.autonomous_pipeline import AutonomousPipeline, load_settings


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
