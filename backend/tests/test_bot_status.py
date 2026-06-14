import os

os.environ.setdefault("LLM_API_KEY", "dummy-test-key")

from app.services.polymarket.autonomous_pipeline import get_bot_status


def test_get_bot_status_structure():
    status = get_bot_status()
    assert "running" in status
    assert "settings" in status
    assert "last_run" in status
    assert "health" in status
    health = status["health"]
    assert "stats" in health
    assert "drawdown" in health
    assert "heat" in health
    assert "alerts" in health
    assert isinstance(health["alerts"], list)
