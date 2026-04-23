"""
Fully autonomous Polymarket paper-trading bot.

Flow per cycle:
  1. Fetch top active markets from Polymarket
  2. Skip markets with open positions
  3. For each market: search web for recent news
  4. LLM estimates YES probability from the news
  5. If edge >= min_edge and confidence is acceptable → open paper position
  6. Refresh prices of existing open positions
  7. Auto-close positions that hit take-profit or stop-loss thresholds

Everything is saved to portfolio_db (JSON files, no extra DB needed).
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from .market_fetcher import MarketFetcher
from .news_researcher import NewsResearcher
from .paper_trader import PaperTrader
from .portfolio_db import PortfolioDatabase
from ...utils.logger import get_logger

logger = get_logger("mirofish.polymarket.autonomous")

# ── Default settings ──────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "max_markets_per_cycle": 10,
    "position_size_usdc": 200.0,
    "min_edge": 0.07,
    "min_confidence": ["high", "medium"],   # skip low-confidence trades
    "take_profit": 0.20,
    "stop_loss": -0.15,
    "cycle_interval_minutes": 30,
    "max_open_positions": 8,
    "min_volume": 5000,
    "min_liquidity": 1000,
    "min_entry_price": 0.05,       # skip penny markets (< 5% probability)
    "stoploss_cooldown_days": 7,   # don't re-enter a market for 7 days after stop-loss
    "takeprofit_cooldown_days": 3, # don't re-enter a market for 3 days after take-profit
    "auto_close": True,
    "delay_between_markets": 8,
}

# Where run logs are stored
LOG_DIR = os.path.join(os.path.dirname(__file__), "../../../uploads/paper_trading")
LOG_FILE = os.path.join(LOG_DIR, "bot_runs.json")
SETTINGS_FILE = os.path.join(LOG_DIR, "bot_settings.json")

_bot_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


# ── Settings helpers ──────────────────────────────────────────────────────────

def load_settings() -> dict:
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        return {**DEFAULT_SETTINGS, **saved}
    return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ── Run log helpers ───────────────────────────────────────────────────────────

def _load_runs() -> list:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_run(run: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    runs = _load_runs()
    runs.insert(0, run)
    runs = runs[:100]  # keep last 100 runs
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(runs, f, ensure_ascii=False, indent=2)


def get_runs(limit: int = 20) -> list:
    return _load_runs()[:limit]


# ── Core cycle ────────────────────────────────────────────────────────────────

class AutonomousPipeline:
    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or load_settings()
        self.fetcher = MarketFetcher()
        self.researcher = NewsResearcher()
        self.trader = PaperTrader()
        self.db = PortfolioDatabase()

    def run_cycle(self) -> dict:
        """
        Executes one full bot cycle. Returns a run summary dict.
        """
        run_id = uuid.uuid4().hex[:8]
        started_at = datetime.now(timezone.utc).isoformat()
        log_entries = []
        trades_opened = 0
        trades_closed = 0
        markets_scanned = 0
        errors = 0

        def log(msg: str, level: str = "info"):
            entry = {
                "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "level": level,
                "msg": msg,
            }
            log_entries.append(entry)
            getattr(logger, level, logger.info)(f"[{run_id}] {msg}")

        try:
            log("🤖 Bot cycle started")
            portfolio = self.db.get_portfolio()
            open_pos = portfolio.get("positions", {})
            balance = portfolio.get("balance", 0)

            # ── Step 1: Auto-close positions that hit TP/SL ──────────────────
            if self.settings.get("auto_close") and open_pos:
                log(f"Checking {len(open_pos)} open positions for TP/SL…")
                for market_id, pos in list(open_pos.items()):
                    pnl_pct = (pos.get("current_value", pos["cost_basis"]) - pos["cost_basis"]) / pos["cost_basis"]
                    tp = self.settings.get("take_profit", 0.20)
                    sl = self.settings.get("stop_loss", -0.15)
                    if pnl_pct >= tp:
                        log(f"✅ TP hit on {pos['question'][:50]} (+{pnl_pct*100:.1f}%) — closing")
                        try:
                            self.trader.close_position(market_id, pos["current_price"], reason="take_profit")
                            trades_closed += 1
                        except Exception as e:
                            log(f"Close error: {e}", "error")
                            errors += 1
                    elif pnl_pct <= sl:
                        log(f"🛑 SL hit on {pos['question'][:50]} ({pnl_pct*100:.1f}%) — closing")
                        try:
                            self.trader.close_position(market_id, pos["current_price"], reason="stop_loss")
                            trades_closed += 1
                        except Exception as e:
                            log(f"Close error: {e}", "error")
                            errors += 1

                # refresh portfolio after closes
                portfolio = self.db.get_portfolio()
                open_pos = portfolio.get("positions", {})
                balance = portfolio.get("balance", 0)

            # ── Step 2: Check capacity ────────────────────────────────────────
            max_pos = self.settings.get("max_open_positions", 8)
            if len(open_pos) >= max_pos:
                log(f"Max positions ({max_pos}) reached — skipping new trades")
            elif balance < self.settings.get("position_size_usdc", 200):
                log(f"Insufficient balance (${balance:.2f}) — skipping new trades")
            else:
                # ── Step 3: Fetch markets ─────────────────────────────────────
                n = self.settings.get("max_markets_per_cycle", 15)
                log(f"Fetching top {n} markets from Polymarket…")
                markets = self.fetcher.get_active_markets(
                    limit=n,
                    min_volume=self.settings.get("min_volume", 5000),
                    min_liquidity=self.settings.get("min_liquidity", 1000),
                )
                log(f"Got {len(markets)} markets")
                markets_scanned = len(markets)

                # ── Step 4: Research + signal + trade ────────────────────────
                # Build cooldown list: markets closed recently (stop-loss OR take-profit)
                cooldown_days = self.settings.get("stoploss_cooldown_days", 7)
                tp_cooldown_days = self.settings.get("takeprofit_cooldown_days", 3)
                closed_trades = self.db.get_trades()
                from datetime import timedelta
                now_utc = datetime.now(timezone.utc)
                cooled_ids = set()
                for t in closed_trades:
                    reason = t.get("reason", "")
                    days = cooldown_days if reason == "stop_loss" else tp_cooldown_days if reason == "take_profit" else 0
                    if days > 0:
                        try:
                            closed_at = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
                            if (now_utc - closed_at) < timedelta(days=days):
                                cooled_ids.add(t["market_id"])
                        except Exception:
                            pass

                for market in markets:
                    if market.id in open_pos:
                        continue
                    if market.id in cooled_ids:
                        log(f"   ⏳ Skipping {market.question[:50]} — recently closed cooldown")
                        continue
                    if len(self.db.get_portfolio().get("positions", {})) >= max_pos:
                        log("Max positions reached mid-cycle — stopping")
                        break

                    # Skip markets where the lower-probability side is below min_entry_price.
                    # In a binary market YES+NO≈1, so if min(yes,no) < 0.05 it means
                    # one outcome is < 5% — betting on it is a lottery ticket.
                    min_price = self.settings.get("min_entry_price", 0.05)
                    if min(market.yes_price, market.no_price) < min_price:
                        log(f"   💸 Skipping penny market: {market.question[:50]} "
                            f"(yes={market.yes_price:.4f} no={market.no_price:.4f})")
                        continue

                    log(f"🔍 Researching: {market.question[:60]}…")
                    # Pace LLM calls to stay within rate limits
                    delay_s = self.settings.get("delay_between_markets", 5)
                    if delay_s > 0:
                        time.sleep(delay_s)
                    try:
                        signal = self.researcher.research(market.question)
                    except Exception as e:
                        log(f"Research error: {e}", "warning")
                        errors += 1
                        continue

                    prob = signal.get("probability")
                    confidence = signal.get("confidence", "irrelevant")
                    relevant = signal.get("relevant", False)

                    reasoning_short = signal.get("reasoning", "")[:80]
                    if not relevant or prob is None:
                        log(f"   → Irrelevant: {reasoning_short}")
                        continue

                    log(f"   → prob={prob:.2f} conf={confidence} | {reasoning_short}")

                    if confidence not in self.settings.get("min_confidence", ["high", "medium", "low"]):
                        log(f"   → Confidence '{confidence}' below threshold — skip")
                        continue

                    # edge check
                    edge = prob - market.yes_price
                    min_edge = self.settings.get("min_edge", 0.07)
                    if abs(edge) < min_edge:
                        log(f"   → No edge (est={prob:.2f} vs mkt={market.yes_price:.2f}, edge={abs(edge):.3f})")
                        continue

                    side = "YES" if edge > 0 else "NO"
                    entry_price = market.yes_price if side == "YES" else market.no_price

                    # Secondary safety: entry price itself must be above threshold
                    if entry_price < min_price:
                        log(f"   💸 Entry price too low for {side}: {entry_price:.4f} — skip")
                        continue

                    log(
                        f"   ✅ EDGE FOUND: {side} | est={prob:.2f} mkt={market.yes_price:.2f} "
                        f"edge={abs(edge):.3f} conf={confidence}"
                    )
                    log(f"   💡 {signal.get('reasoning', '')}")

                    try:
                        bal = self.db.get_portfolio().get("balance", 0)
                        size = min(self.settings.get("position_size_usdc", 200), bal)
                        self.trader.open_position(
                            market_id=market.id,
                            question=market.question,
                            side=side,
                            entry_price=entry_price,
                            amount_usdc=size,
                            estimated_prob=prob,
                            confidence=confidence,
                            reasoning=signal.get("reasoning", ""),
                        )
                        trades_opened += 1
                        log(f"   💰 Position opened: {side} ${size:.0f} @ {entry_price:.4f}")
                    except Exception as e:
                        log(f"   Trade error: {e}", "error")
                        errors += 1

            # ── Step 5: Refresh open position prices ──────────────────────────
            portfolio = self.db.get_portfolio()
            open_pos = portfolio.get("positions", {})
            if open_pos:
                log(f"Refreshing prices for {len(open_pos)} open positions…")
                try:
                    all_markets = self.fetcher.get_active_markets(limit=100)
                    mkt_map = {m.id: m for m in all_markets}
                    for market_id, pos in open_pos.items():
                        mkt = mkt_map.get(market_id)
                        if mkt:
                            token_id = mkt.yes_token_id if pos["side"] == "YES" else mkt.no_token_id
                            if token_id:
                                price = self.fetcher.get_market_price(token_id)
                                if price is not None:
                                    self.trader.update_position_price(market_id, price)
                except Exception as e:
                    log(f"Price refresh error: {e}", "warning")

        except Exception as e:
            log(f"Cycle error: {e}", "error")
            errors += 1

        finished_at = datetime.now(timezone.utc).isoformat()
        stats = self.db.get_stats()

        run = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "markets_scanned": markets_scanned,
            "trades_opened": trades_opened,
            "trades_closed": trades_closed,
            "errors": errors,
            "portfolio_value": stats["total_value"],
            "total_pnl": stats["total_pnl"],
            "open_positions": stats["open_positions"],
            "log": log_entries,
        }
        _save_run(run)
        log(f"✅ Cycle done: {trades_opened} opened, {trades_closed} closed, {errors} errors")
        return run


# ── Background scheduler ─────────────────────────────────────────────────────

def get_bot_status() -> dict:
    global _bot_thread
    running = _bot_thread is not None and _bot_thread.is_alive()
    settings = load_settings()
    return {
        "running": running,
        "settings": settings,
        "last_run": (get_runs(1) or [None])[0],
    }


def start_bot():
    global _bot_thread, _stop_event
    if _bot_thread and _bot_thread.is_alive():
        return {"started": False, "reason": "Bot is already running"}

    _stop_event.clear()

    def loop():
        logger.info("Background bot started")
        while not _stop_event.is_set():
            try:
                settings = load_settings()
                pipeline = AutonomousPipeline(settings)
                pipeline.run_cycle()
                interval = settings.get("cycle_interval_minutes", 30) * 60
                logger.info(f"Bot sleeping {interval // 60}m until next cycle…")
                _stop_event.wait(timeout=interval)
            except Exception as e:
                logger.error(f"Bot loop error: {e}")
                _stop_event.wait(timeout=60)
        logger.info("Background bot stopped")

    _bot_thread = threading.Thread(target=loop, daemon=True, name="polymarket-bot")
    _bot_thread.start()
    return {"started": True}


def stop_bot():
    global _stop_event
    _stop_event.set()
    return {"stopped": True}


def run_once() -> dict:
    """Run a single cycle synchronously (for manual trigger)."""
    settings = load_settings()
    pipeline = AutonomousPipeline(settings)
    return pipeline.run_cycle()
