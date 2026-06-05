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
from datetime import datetime, timezone, timedelta
from typing import Optional

from .market_fetcher import MarketFetcher
from .news_researcher import NewsResearcher
from .paper_trader import PaperTrader
from .portfolio_db import PortfolioDatabase
from .bot_reporter import BotReporter
from ...utils.logger import get_logger

logger = get_logger("mirofish.polymarket.autonomous")

# ── Default settings ──────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "max_markets_per_cycle": 10,
    "position_size_usdc": 200.0,       # REDUCED: 400→200 — protect damaged portfolio
    "min_edge": 0.10,                  # council Tier 1: stronger signal
    "min_confidence": ["high", "medium"],
    "take_profit": 0.20,
    "stop_loss": -0.15,                # base SL; actual SL is tiered by entry price
    "stop_loss_low_entry": -0.20,      # entry < 30%
    "stop_loss_high_entry": -0.10,     # entry > 70%
    "cycle_interval_minutes": 30,
    "max_open_positions": 5,
    "min_volume": 5000,
    "min_liquidity": 1000,
    "min_entry_price": 0.01,           # allow extreme contrarian entries (1-15% range)
    "excluded_market_keywords": [
        "nba", "nfl", "nhl", "mlb", "mls",
        "premier league", "la liga", "bundesliga", "serie a", "ligue 1", "champions league",
        "world cup", "euro ", "copa", "league cup",
        "super bowl", "stanley cup", "world series",
        "match", "vs.", " vs ", "game 1", "game 2", "game 3", "game 4", "game 5", "game 6", "game 7",
        "o/u ", "over/under", "spread",
        "lpl ", "lck ", "esports", "esport",
    ],
    "stoploss_cooldown_days": 7,
    "takeprofit_cooldown_days": 3,
    "auto_close": True,
    "delay_between_markets": 8,
    "max_days_to_expiry": 30,
    "long_term_position_ratio": 0.40,
    "pre_expiry_lock_days": 5,
    "min_days_to_expiry_entry": 7,
    "stale_position_days": 7,          # RE-ENABLED: 5→7d — contrarian needs time, but not forever
    "stale_position_movement": 0.03,
    # Tier 2: dynamic position sizing multipliers
    "size_multiplier_high_conf_large_edge": 1.00,  # high conf + edge ≥20% → $200
    "size_multiplier_high_conf_base": 0.75,        # high conf + edge 10-20% → $150
    "size_multiplier_medium_conf_large_edge": 0.75,# medium conf + edge ≥20% → $150
    "size_multiplier_medium_conf_base": 0.50,      # medium conf + edge 10-20% → $100
    # ── RISK MANAGEMENT v3 ──
    "max_portfolio_risk_pct": 0.15,    # max 15% of portfolio at risk across open positions
    "max_drawdown_pause_pct": 0.20,    # pause new entries if drawdown >20%
    "drawdown_reduce_sizing_pct": 0.10,# reduce sizing 50% if drawdown >10%
    "hard_stop_pp_extreme": 0.05,      # entries <10% or >90% -> close if dominant side moves +5pp
    "hard_stop_pp_mid": 0.07,          # entries <20% or >80% -> close if dominant side moves +7pp
    "hard_stop_pp_standard": 0.10,     # standard: close if dominant side moves +10pp
    "sl_extreme_tight": -0.10,         # entries <10% or >90% -> SL -10% (was -25%)
    "sl_extreme_mid": -0.15,           # entries <20% or >80% -> SL -15% (was -20%)
    "sl_standard": -0.15,              # standard SL
}

# Where run logs are stored
LOG_DIR = os.path.join(os.path.dirname(__file__), "../../../uploads/paper_trading")
LOG_FILE = os.path.join(LOG_DIR, "bot_runs.json")
SETTINGS_FILE = os.path.join(LOG_DIR, "bot_settings.json")

_bot_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
# Use /tmp for debounce file so it does NOT survive container restarts.
# If it were in LOG_DIR (persistent volume), Railway restarts would
# see a fresh timestamp and refuse to start the bot forever.
_LAST_START_FILE = "/tmp/.bot_last_start"


# ── Settings helpers ──────────────────────────────────────────────────────────

def load_settings() -> dict:
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        merged = {**DEFAULT_SETTINGS, **saved}
    else:
        merged = DEFAULT_SETTINGS.copy()

    # ── Hard floor: saved settings (e.g. from old UI) can never go BELOW these ──
    # This ensures code-level upgrades take effect even when bot_settings.json
    # has stale values from a previous deploy.
    FLOOR = {
        "min_entry_price": 0.01,          # contrarian strategy — allow extreme entries < 5%
        "min_edge": 0.10,                 # council Tier 1 — never below 10%
        "min_days_to_expiry_entry": 7,    # Tier 2 — hard block, never disable
        "pre_expiry_lock_days": 5,        # Tier 2 — protect profits, never below 5d
    }
    for key, floor_val in FLOOR.items():
        if merged.get(key, 0) < floor_val:
            merged[key] = floor_val

    # ── Hard overrides: these values always come from code, never from saved file ──
    HARDCODE = {
        "position_size_usdc": 200.0,     # REDUCED — protect capital
        "max_days_to_expiry": 30,
        "min_entry_price": 0.01,
        "max_portfolio_risk_pct": 0.15,
        "max_drawdown_pause_pct": 0.20,
        "drawdown_reduce_sizing_pct": 0.10,
        "hard_stop_pp_extreme": 0.05,
        "hard_stop_pp_mid": 0.07,
        "hard_stop_pp_standard": 0.10,
        "sl_extreme_tight": -0.10,
        "sl_extreme_mid": -0.15,
        "sl_standard": -0.15,
    }
    for key, val in HARDCODE.items():
        merged[key] = val

    # ── Additive defaults: list settings not present in saved file get defaults ──
    for key in ("excluded_market_keywords",):
        if key not in merged or not merged[key]:
            merged[key] = DEFAULT_SETTINGS.get(key, [])

    return merged


def save_settings(settings: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ── Run log helpers ───────────────────────────────────────────────────────────

def _load_runs() -> list:
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        # Corrupted file — reset it
        try:
            os.remove(LOG_FILE)
        except Exception:
            pass
        return []


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
    # Class-level cycle counter so it survives across AutonomousPipeline instances
    # (the background loop creates a new instance every cycle)
    _cycle_count = 0

    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or load_settings()
        self.fetcher = MarketFetcher()
        self.researcher = NewsResearcher()
        self.trader = PaperTrader()
        self.db = PortfolioDatabase()
        self.reporter = BotReporter(LOG_DIR)

    def _detect_disruption(self, question: str, yes_price: float, no_price: float) -> tuple[bool, list[str]]:
        """
        Searches for recent news and asks the LLM whether there's a disruption
        that invalidates the current extreme consensus.
        Returns (disruption_detected, news_headlines)
        """
        current_year = datetime.now(timezone.utc).year
        query = f"{question[:100].strip()} {current_year}"
        try:
            snippets = self.researcher._search(query, 5)
        except Exception as e:
            logger.warning(f"News search failed: {e}")
            snippets = []

        news_summary = []
        for s in snippets[:5]:
            title = s.get("title", "")
            if title:
                news_summary.append(f"- {title}")

        news_text = "\n".join(news_summary) if news_summary else "No recent news found."

        llm_prompt = f"""Market: {question}
Current market price: YES at {yes_price:.1%}, NO at {no_price:.1%}

Recent news headlines:
{news_text}

Task: Determine if there is a STRONG DISRUPTIVE event in the last 48 hours that would invalidate the current extreme market consensus.
- If YES > 85% and market assumes certainty: Is there NEW evidence that makes the NO outcome likely?
- If NO > 85% and market assumes certainty: Is there NEW evidence that makes the YES outcome likely?

Respond ONLY with one of:
- "DISRUPTION: YES" (news strongly favor the underdog / contradict consensus)
- "DISRUPTION: NO" (news strongly favor the favorite / maintain consensus)
- "NO DISRUPTION" (no significant new information)

Do NOT estimate probabilities. Do NOT give percentages. Do NOT explain reasoning.
"""
        try:
            raw = self.researcher.llm.chat(
                [{"role": "user", "content": llm_prompt}],
                temperature=0.2,
                max_tokens=100,
            )
            llm_response = raw.strip().upper()
            disruption = "DISRUPTION: YES" in llm_response
            return disruption, news_summary
        except Exception as e:
            logger.warning(f"LLM disruption check failed: {e}")
            return False, news_summary

    def _get_portfolio_drawdown(self) -> float:
        """Returns current drawdown from peak equity (0.0 = no drawdown)."""
        equity = self.db.get_equity_history()
        if not equity:
            return 0.0
        peak = max(e.get("value", 0) for e in equity)
        current = self.db.get_stats()["total_value"]
        if peak <= 0:
            return 0.0
        return (peak - current) / peak

    def _get_portfolio_heat(self) -> float:
        """Returns fraction of portfolio currently at risk in open positions."""
        portfolio = self.db.get_portfolio()
        positions = portfolio.get("positions", {})
        if not positions:
            return 0.0
        total_cost = sum(pos.get("cost_basis", 0) for pos in positions.values())
        total_value = self.db.get_stats()["total_value"]
        if total_value <= 0:
            return 0.0
        return total_cost / total_value

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

            # ── Step 1: Auto-close positions that hit TP/SL or are expired ──────
            if self.settings.get("auto_close") and open_pos:
                log(f"Checking {len(open_pos)} open positions for TP/SL/expiry/hard-stop…")
                # Pre-fetch all markets for price refresh + hard-stop calculation
                try:
                    all_markets = self.fetcher.get_active_markets(limit=200)
                    mkt_map = {m.id: m for m in all_markets}
                except Exception:
                    mkt_map = {}

                for market_id, pos in list(open_pos.items()):
                    pnl_pct = (pos.get("current_value", pos["cost_basis"]) - pos["cost_basis"]) / pos["cost_basis"]
                    tp = self.settings.get("take_profit", 0.20)
                    entry_p = pos.get("entry_price", 0.5)
                    side = pos.get("side", "YES")

                    # ── RISK v3: Tiered stop-loss ──
                    # Tighter SL for extreme entries — capital preservation is #1
                    if entry_p < 0.10 or entry_p > 0.90:
                        sl = self.settings.get("sl_extreme_tight", -0.10)
                    elif entry_p < 0.20 or entry_p > 0.80:
                        sl = self.settings.get("sl_extreme_mid", -0.15)
                    else:
                        sl = self.settings.get("sl_standard", -0.15)

                    # Refresh current price from live market data before any decision
                    mkt = mkt_map.get(market_id)
                    live_price = pos.get("current_price", entry_p)
                    if mkt:
                        token_id = mkt.yes_token_id if side == "YES" else mkt.no_token_id
                        if token_id:
                            fetched = self.fetcher.get_market_price(token_id)
                            if fetched is not None:
                                live_price = fetched
                                self.trader.update_position_price(market_id, live_price)
                                # Recalculate pnl with live price
                                shares = pos.get("shares", 0)
                                live_value = shares * live_price
                                pnl_pct = (live_value - pos["cost_basis"]) / pos["cost_basis"] if pos["cost_basis"] > 0 else 0

                    # Auto-close if market end_date has passed (+ 4h grace)
                    try:
                        end_date_str = pos.get("end_date", "")
                        if end_date_str:
                            from dateutil import parser as dateparser
                            end_dt = dateparser.parse(end_date_str)
                            if end_dt and end_dt.tzinfo is None:
                                end_dt = end_dt.replace(tzinfo=timezone.utc)
                            if end_dt and datetime.now(timezone.utc) > end_dt + timedelta(hours=4):
                                log(f"⏰ Market expired on {end_date_str[:10]}: {pos['question'][:50]}")
                                self.trader.close_position(market_id, live_price, reason="expired")
                                trades_closed += 1
                                continue
                    except Exception:
                        pass

                    # Fallback: auto-close positions open more than 14 days
                    try:
                        opened_at = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
                        days_open = (datetime.now(timezone.utc) - opened_at).days
                        if days_open >= 14:
                            log(f"⏰ Closing zombie position ({days_open}d): {pos['question'][:50]}")
                            self.trader.close_position(market_id, live_price, reason="expired")
                            trades_closed += 1
                            continue
                    except Exception:
                        pass

                    # Pre-expiry profit lock
                    pre_lock_days = self.settings.get("pre_expiry_lock_days", 3)
                    try:
                        end_date_str = pos.get("end_date", "")
                        if end_date_str and pre_lock_days > 0 and pnl_pct > 0:
                            from dateutil import parser as dateparser
                            end_dt = dateparser.parse(end_date_str)
                            if end_dt and end_dt.tzinfo is None:
                                end_dt = end_dt.replace(tzinfo=timezone.utc)
                            if end_dt:
                                days_left = (end_dt - datetime.now(timezone.utc)).days
                                if days_left <= pre_lock_days:
                                    log(f"🔒 Pre-expiry lock ({days_left}d left, +{pnl_pct*100:.1f}%): {pos['question'][:50]}")
                                    self.trader.close_position(market_id, live_price, reason="pre_expiry_lock")
                                    trades_closed += 1
                                    continue
                    except Exception:
                        pass

                    # ── RISK v3: Hard stop if dominant side moved against us ──
                    # Dynamic hard stop: tighter for extreme entries (less room to be wrong)
                    entry_p = pos.get("entry_price", 0.5)
                    if entry_p < 0.10 or entry_p > 0.90:
                        hard_stop_pp = self.settings.get("hard_stop_pp_extreme", 0.05)
                    elif entry_p < 0.20 or entry_p > 0.80:
                        hard_stop_pp = self.settings.get("hard_stop_pp_mid", 0.07)
                    else:
                        hard_stop_pp = self.settings.get("hard_stop_pp_standard", 0.10)

                    if mkt and hard_stop_pp > 0:
                        yes_price = getattr(mkt, 'yes_price', 0.5)
                        no_price = getattr(mkt, 'no_price', 0.5)
                        dominant_moved = False
                        if side == "NO":
                            # We bought NO. If YES (dominant) went UP, that's against us.
                            # Entry: YES was at (1 - entry_p). Current YES = yes_price.
                            yes_at_entry = 1.0 - entry_p
                            if yes_price - yes_at_entry >= hard_stop_pp:
                                dominant_moved = True
                                move_pp = yes_price - yes_at_entry
                        else:  # side == "YES"
                            # We bought YES. If NO (dominant) went UP, that's against us.
                            no_at_entry = 1.0 - entry_p
                            if no_price - no_at_entry >= hard_stop_pp:
                                dominant_moved = True
                                move_pp = no_price - no_at_entry
                        if dominant_moved:
                            log(f"🚨 HARD STOP: dominant side moved +{move_pp*100:.1f}pp against us — {pos['question'][:50]}")
                            self.trader.close_position(market_id, live_price, reason="hard_stop")
                            trades_closed += 1
                            continue

                    # ── RISK v3: Stale close (re-enabled, contrarian-aware) ──
                    # If after N days the market is still at the same extreme (or more),
                    # the contrarian thesis is not working — free the capital.
                    stale_days = self.settings.get("stale_position_days", 7)
                    stale_move = self.settings.get("stale_position_movement", 0.03)
                    try:
                        opened_at = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
                        days_open = (datetime.now(timezone.utc) - opened_at).days
                        if days_open >= stale_days:
                            entry_p = pos.get("entry_price", pos.get("current_price", 0))
                            curr_p = live_price
                            if entry_p > 0:
                                movement = abs(curr_p - entry_p) / entry_p
                                # For contrarian: if price moved TOWARD the dominant side,
                                # that's against us — close even if movement is small.
                                # If price moved AWAY (our favor), that's good — don't close.
                                if side == "YES" and curr_p < entry_p:
                                    # YES went down (market favors NO more) — against us
                                    log(f"💤 Stale+against ({days_open}d, YES fell {movement*100:.1f}%) — cutting: {pos['question'][:50]}")
                                    self.trader.close_position(market_id, live_price, reason="stale")
                                    trades_closed += 1
                                    continue
                                elif side == "NO" and curr_p < entry_p:
                                    # NO went down (market favors YES more) — against us
                                    log(f"💤 Stale+against ({days_open}d, NO fell {movement*100:.1f}%) — cutting: {pos['question'][:50]}")
                                    self.trader.close_position(market_id, live_price, reason="stale")
                                    trades_closed += 1
                                    continue
                                elif movement < stale_move:
                                    # No significant movement at all — free capital
                                    log(f"💤 Stale trade ({days_open}d, {movement*100:.1f}% move) — freeing capital: {pos['question'][:50]}")
                                    self.trader.close_position(market_id, live_price, reason="stale")
                                    trades_closed += 1
                                    continue
                    except Exception:
                        pass

                    if pnl_pct >= tp:
                        log(f"✅ TP hit on {pos['question'][:50]} (+{pnl_pct*100:.1f}%) — closing")
                        try:
                            self.trader.close_position(market_id, live_price, reason="take_profit")
                            trades_closed += 1
                        except Exception as e:
                            log(f"Close error: {e}", "error")
                            errors += 1
                    elif pnl_pct <= sl:
                        log(f"🛑 SL hit on {pos['question'][:50]} ({pnl_pct*100:.1f}%) — closing")
                        try:
                            self.trader.close_position(market_id, live_price, reason="stop_loss")
                            trades_closed += 1
                        except Exception as e:
                            log(f"Close error: {e}", "error")
                            errors += 1

                # refresh portfolio after closes
                portfolio = self.db.get_portfolio()
                open_pos = portfolio.get("positions", {})
                balance = portfolio.get("balance", 0)

            # ── Step 2: Check capacity + risk limits ─────────────────────────
            max_pos = self.settings.get("max_open_positions", 8)
            drawdown = self._get_portfolio_drawdown()
            heat = self._get_portfolio_heat()
            max_dd_pause = self.settings.get("max_drawdown_pause_pct", 0.20)
            max_heat = self.settings.get("max_portfolio_risk_pct", 0.15)

            if len(open_pos) >= max_pos:
                log(f"Max positions ({max_pos}) reached — skipping new trades")
            elif balance < self.settings.get("position_size_usdc", 200):
                log(f"Insufficient balance (${balance:.2f}) — skipping new trades")
            elif drawdown >= max_dd_pause:
                log(f"🛑 DRAWDOWN PAUSE: dd={drawdown*100:.1f}% ≥ {max_dd_pause*100:.1f}% — no new entries")
            elif heat >= max_heat:
                log(f"🛑 HEAT LIMIT: {heat*100:.1f}% of portfolio at risk ≥ {max_heat*100:.1f}% — no new entries")
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

                # Pre-build excluded keywords list (lower-cased once per cycle)
                excluded_kw = [kw.lower() for kw in self.settings.get("excluded_market_keywords", [])]

                for market in markets:
                    if market.id in open_pos:
                        continue
                    if market.id in cooled_ids:
                        log(f"   ⏳ Skipping {market.question[:50]} — recently closed cooldown")
                        continue

                    # Council Tier 1: skip sports/hyper-efficient markets
                    if excluded_kw:
                        q_lower = market.question.lower()
                        hit = next((kw for kw in excluded_kw if kw in q_lower), None)
                        if hit:
                            log(f"   🚫 Sports/excluded keyword '{hit}': {market.question[:50]}")
                            continue
                    if len(self.db.get_portfolio().get("positions", {})) >= max_pos:
                        log("Max positions reached mid-cycle — stopping")
                        break

                    # 70/30 long-term cap: allow at most 30% of slots for markets
                    # expiring more than max_days_to_expiry days away.
                    max_days = self.settings.get("max_days_to_expiry", 60)
                    lt_ratio = self.settings.get("long_term_position_ratio", 0.30)
                    max_lt = max(1, int(max_pos * lt_ratio))
                    if max_days and market.end_date:
                        try:
                            from dateutil import parser as dateparser
                            end_dt = dateparser.parse(market.end_date)
                            if end_dt and end_dt.tzinfo is None:
                                end_dt = end_dt.replace(tzinfo=timezone.utc)
                            if end_dt:
                                days_left = (end_dt - datetime.now(timezone.utc)).days
                                if days_left > max_days:
                                    # Count existing long-term open positions
                                    lt_open = 0
                                    for pos in open_pos.values():
                                        ed = pos.get("end_date", "")
                                        if ed:
                                            try:
                                                pe = dateparser.parse(ed)
                                                if pe and pe.tzinfo is None:
                                                    pe = pe.replace(tzinfo=timezone.utc)
                                                if pe and (pe - datetime.now(timezone.utc)).days > max_days:
                                                    lt_open += 1
                                            except Exception:
                                                pass
                                    if lt_open >= max_lt:
                                        log(f"   📅 Long-term cap ({lt_open}/{max_lt}) — skipping {days_left}d: {market.question[:50]}")
                                        continue
                                    else:
                                        log(f"   📅 Long-term slot {lt_open+1}/{max_lt} ({days_left}d): {market.question[:50]}")
                        except Exception:
                            pass

                    # Tier 2: hard block on short-expiry markets.
                    # Near-expiry markets are already priced by the crowd — LLM has no edge.
                    # Markets without an end_date are allowed through (open-ended questions).
                    min_entry_days = self.settings.get("min_days_to_expiry_entry", 7)
                    if min_entry_days and market.end_date:
                        try:
                            from dateutil import parser as dateparser
                            end_dt = dateparser.parse(market.end_date)
                            if end_dt and end_dt.tzinfo is None:
                                end_dt = end_dt.replace(tzinfo=timezone.utc)
                            if end_dt:
                                days_left = (end_dt - datetime.now(timezone.utc)).days
                                if days_left < min_entry_days:
                                    log(f"   📅 Skipping short-expiry {days_left}d (min={min_entry_days}d): {market.question[:50]}")
                                    continue
                        except Exception:
                            pass

                    # === NUEVO: Filtro de extremos (contrarian) ===
                    # Si ambos lados están entre 15% y 85%, SKIP. No hay edge contrarian.
                    if (0.15 < market.yes_price < 0.85) and (0.15 < market.no_price < 0.85):
                        log(f"   ⏭️ Skip middle range (YES={market.yes_price:.2%}, NO={market.no_price:.2%}) — no contrarian edge")
                        continue

                    log(f"🔍 Researching: {market.question[:60]}…")
                    # Pace LLM calls to stay within rate limits
                    delay_s = self.settings.get("delay_between_markets", 5)
                    if delay_s > 0:
                        time.sleep(delay_s)
                    try:
                        disruption, news_summary = self._detect_disruption(
                            market.question, market.yes_price, market.no_price
                        )
                    except Exception as e:
                        log(f"Disruption detection error: {e}", "warning")
                        errors += 1
                        continue

                    # === NUEVA LÓGICA DE DECISIÓN (contrarian pura) ===
                    side = None
                    entry_price = None
                    confidence = None
                    edge = 0.0

                    if market.yes_price > 0.85 and not disruption:
                        side = "NO"
                        entry_price = market.no_price
                        confidence = "high"
                        edge = market.yes_price - 0.85
                    elif market.no_price > 0.85 and not disruption:
                        side = "YES"
                        entry_price = market.yes_price
                        confidence = "high"
                        edge = market.no_price - 0.85
                    elif disruption:
                        log(f"   ⚠️ Disruption detected — market may be adjusting, skip")
                        logger.info(json.dumps({
                            "event": "CONTRARIAN_EVAL",
                            "market_id": market.id,
                            "question": market.question[:80],
                            "yes_price": round(market.yes_price, 4),
                            "no_price": round(market.no_price, 4),
                            "side": None,
                            "disruption": True,
                            "edge": 0,
                            "size": 0,
                            "decision": "SKIP",
                            "reason": "disruption detected",
                        }))
                        continue
                    else:
                        log(f"   ⏭️ Not in extreme range — skip")
                        logger.info(json.dumps({
                            "event": "CONTRARIAN_EVAL",
                            "market_id": market.id,
                            "question": market.question[:80],
                            "yes_price": round(market.yes_price, 4),
                            "no_price": round(market.no_price, 4),
                            "side": None,
                            "disruption": False,
                            "edge": 0,
                            "size": 0,
                            "decision": "SKIP",
                            "reason": "not in extreme range",
                        }))
                        continue

                    if edge < 0.05:
                        log(f"   ⏭️ Edge too small ({edge:.2%}) — skip")
                        logger.info(json.dumps({
                            "event": "CONTRARIAN_EVAL",
                            "market_id": market.id,
                            "question": market.question[:80],
                            "yes_price": round(market.yes_price, 4),
                            "no_price": round(market.no_price, 4),
                            "side": side,
                            "disruption": False,
                            "edge": round(edge, 4),
                            "size": 0,
                            "decision": "SKIP",
                            "reason": "edge too small",
                        }))
                        continue

                    # Secondary safety: entry price itself must be above threshold
                    min_price = self.settings.get("min_entry_price", 0.05)
                    if entry_price < min_price:
                        log(f"   💸 Entry price too low for {side}: {entry_price:.4f} — skip")
                        logger.info(json.dumps({
                            "event": "CONTRARIAN_EVAL",
                            "market_id": market.id,
                            "question": market.question[:80],
                            "yes_price": round(market.yes_price, 4),
                            "no_price": round(market.no_price, 4),
                            "side": side,
                            "disruption": False,
                            "edge": round(edge, 4),
                            "size": 0,
                            "decision": "SKIP",
                            "reason": "entry price below min",
                        }))
                        continue

                    log(
                        f"   ✅ CONTRARIAN EDGE: {side} | "
                        f"extreme={'YES' if market.yes_price > 0.85 else 'NO'} "
                        f"edge={edge:.3f} disruption={disruption}"
                    )

                    try:
                        bal = self.db.get_portfolio().get("balance", 0)
                        # === RISK v3: Conservative dynamic sizing ===
                        base_size = self.settings.get("position_size_usdc", 200.0)
                        edge_strength = abs(edge)

                        if edge_strength >= 0.10:
                            multiplier = 1.0   # $200
                        elif edge_strength >= 0.05:
                            multiplier = 0.75  # $150
                        else:
                            multiplier = 0.5   # $100 (fallback)

                        dynamic_size = round(base_size * multiplier)

                        # Drawdown reduction: if down >10%, halve sizing
                        dd_reduce = self.settings.get("drawdown_reduce_sizing_pct", 0.10)
                        if drawdown >= dd_reduce:
                            dynamic_size = round(dynamic_size * 0.5)
                            log(f"   📉 Sizing halved: drawdown {drawdown*100:.1f}%")

                        # CAP: max 5% of total portfolio value per position
                        portfolio_stats = self.db.get_stats()
                        portfolio_value = portfolio_stats.get("total_value", 10000)
                        max_position = portfolio_value * 0.05
                        dynamic_size = min(dynamic_size, max_position)

                        # FLOOR: minimum $100
                        dynamic_size = max(dynamic_size, 100.0)

                        size = min(dynamic_size, bal)
                        self.trader.open_position(
                            market_id=market.id,
                            question=market.question,
                            side=side,
                            entry_price=entry_price,
                            amount_usdc=size,
                            estimated_prob=0.5,  # no longer used meaningfully
                            confidence=confidence,
                            reasoning=f"Extreme contrarian: fade {'YES' if market.yes_price > 0.85 else 'NO'} consensus @ {edge:.1%} excess. News: {'; '.join(news_summary[:3])}",
                            end_date=market.end_date,
                        )
                        trades_opened += 1
                        # Update in-memory open_pos to prevent re-entry in same cycle
                        open_pos[market.id] = {"entry_price": entry_price}
                        effective_mult = round(size / base_size, 2) if base_size > 0 else 0
                        log(f"   💰 Position opened: {side} ${size:.0f} (x{effective_mult}) @ {entry_price:.4f}")
                        logger.info(json.dumps({
                            "event": "CONTRARIAN_EVAL",
                            "market_id": market.id,
                            "question": market.question[:80],
                            "yes_price": round(market.yes_price, 4),
                            "no_price": round(market.no_price, 4),
                            "side": side,
                            "disruption": False,
                            "edge": round(edge, 4),
                            "size": size,
                            "decision": "OPEN",
                            "reason": "extreme contrarian, no disruption",
                        }))
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

        # === Generar reporte automático cada 6 ciclos (~3 horas) ===
        AutonomousPipeline._cycle_count += 1
        if AutonomousPipeline._cycle_count % 6 == 0:
            try:
                report = self.reporter.generate_report()
                logger.info(f"[REPORT] Generated report at {report['generated_at']} — Status: {report['status']}")
                if report["alerts"]:
                    for alert in report["alerts"]:
                        logger.warning(f"[REPORT ALERT] {alert}")
            except Exception as e:
                logger.error(f"[REPORT ERROR] Failed to generate report: {e}")

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

    # ── Debounce logic ─────────────────────────────────────────────────────
    # 30s is enough to prevent double-start within the same process.
    # File lives in /tmp so it is wiped on container restart → bot always
    # starts after a Railway redeploy or crash-recovery.
    os.makedirs(LOG_DIR, exist_ok=True)
    now_ts = time.time()

    # If a thread is already alive, do nothing (idempotent)
    if _bot_thread and _bot_thread.is_alive():
        logger.info("start_bot(): bot thread already alive — skipping")
        return {"started": False, "reason": "already_running"}

    if os.path.exists(_LAST_START_FILE):
        try:
            with open(_LAST_START_FILE, "r", encoding="utf-8") as _f:
                _last = float(_f.read().strip())
            if now_ts - _last < 30:
                logger.warning(
                    f"start_bot() called only {now_ts - _last:.0f}s after last start — skipping duplicate"
                )
                return {"started": False, "reason": "too_soon"}
        except Exception:
            pass
    try:
        with open(_LAST_START_FILE, "w", encoding="utf-8") as _f:
            _f.write(str(now_ts))
    except Exception:
        pass

    # ── Start fresh thread ─────────────────────────────────────────────────
    _stop_event = threading.Event()
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
    logger.info("start_bot(): bot thread launched successfully")
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
