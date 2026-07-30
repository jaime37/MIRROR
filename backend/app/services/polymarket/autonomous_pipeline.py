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

Everything is saved to portfolio_db (SQLite with WAL mode).
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
from .paper_trader import PaperTrader, taker_fee
from .portfolio_db import PortfolioDatabase
from .bot_reporter import BotReporter
from ...utils.logger import get_logger

logger = get_logger("mirofish.polymarket.autonomous")

# Bumped whenever strategy/settings semantics change — stamped on OPEN trades
# and prob_estimates rows so results can be attributed to a config generation.
SETTINGS_VERSION = "v5-swarm-2026-07-30"


def entry_bucket(price: float) -> str:
    """Bucket label for an entry price, used for per-bucket calibration stats."""
    if price < 0.05:
        return "0-0.05"
    if price < 0.10:
        return "0.05-0.10"
    if price < 0.20:
        return "0.10-0.20"
    if price < 0.30:
        return "0.20-0.30"
    if price < 0.70:
        return "0.30-0.70"
    if price < 0.80:
        return "0.70-0.80"
    if price < 0.90:
        return "0.80-0.90"
    if price < 0.95:
        return "0.90-0.95"
    return "0.95-1"

# ── Default settings ──────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "max_markets_per_cycle": 30,
    "position_size_usdc": 150.0,       # REDUCED: 400→200→150 — smaller risk per ticket
    "min_edge": 0.10,                  # council Tier 1: stronger signal
    "min_confidence": ["high", "medium"],
    # Adaptive take-profit by entry price — let extreme contrarian outliers run
    "tp_under_05": 1.00,               # entry < 5% or > 95%
    "tp_under_10": 0.50,               # entry < 10% or > 90%
    "tp_under_20": 0.30,               # entry < 20% or > 80%
    "tp_standard": 0.20,               # standard TP
    # Adaptive stop-loss by entry price (SL re-anchored to bid, wider for extremes;
    # 0 = price-based SL disabled for that bucket)
    "sl_under_05": 0.0,                # entry < 5% or > 95% — price-based SL off
    "sl_under_10": -0.40,              # entry < 10% or > 90%
    "sl_under_20": -0.25,              # entry < 20% or > 80%
    "sl_standard": -0.15,              # standard SL
    "catastrophic_stop_pct": 0.50,     # extreme entries: close if bid <= entry × 0.50
    "execution_mode": "maker",         # "maker" (limit, fee 0) or "taker" (walk book)
    "cycle_interval_minutes": 30,
    "max_open_positions": 5,
    "min_volume": 5000,
    "min_liquidity": 1000,
    "min_entry_price": 0.01,           # allow extreme contrarian entries (underdog >=1%)
    "excluded_market_keywords": [
        "nba", "nfl", "nhl", "mlb", "mls",
        "premier league", "la liga", "bundesliga", "serie a", "ligue 1", "champions league",
        "world cup", "euro ", "copa", "league cup",
        "super bowl", "stanley cup", "world series",
        "match", "vs.", " vs ", "game 1", "game 2", "game 3", "game 4", "game 5", "game 6", "game 7",
        "o/u ", "over/under", "spread",
        "lpl ", "lck ", "esports", "esport",
        "wimbledon", "us open", "french open", "australian open", "tennis",
        "grand slam", "atp", "wta", "fonseca",
    ],
    "stoploss_cooldown_days": 7,
    "takeprofit_cooldown_days": 3,
    "auto_close": True,
    "delay_between_markets": 8,
    "max_days_to_expiry": 60,
    "long_term_position_ratio": 0.40,
    "pre_expiry_lock_days": 5,
    "min_days_to_expiry_entry": 7,
    "stale_position_days": 0,          # 0 = disabled — stale rule off by default
    "stale_position_movement": 0.03,
    "max_position_age_days": 60,       # zombie close only for positions with NO end_date
    # Tier 2: dynamic position sizing multipliers
    "size_multiplier_high_conf_large_edge": 1.25,  # high conf + edge ≥10% → $187.5
    "size_multiplier_high_conf_base": 1.00,        # high conf + edge 5-10% → $150
    "size_multiplier_medium_conf_large_edge": 0.85,# medium conf + edge ≥10% → $127.5
    "size_multiplier_medium_conf_base": 0.70,      # medium conf + edge 5-10% → $105
    # ── RISK MANAGEMENT v3 ──
    "max_portfolio_risk_pct": 0.20,    # max 20% of portfolio at risk across open positions
    "dd_soft_reduce_pct": 0.05,        # reduce sizing 25% if drawdown >5%
    "dd_soft_reduce_factor": 0.75,     # multiplier after soft reduce
    "drawdown_reduce_sizing_pct": 0.10,# reduce sizing 50% if drawdown >10%
    "max_drawdown_pause_pct": 0.15,    # pause new entries if drawdown >15%
    # Scoring & diversification
    "min_opportunity_score": 50.0,     # minimum composite score to open a trade
    "max_positions_per_category": 2,   # avoid concentration in one theme
    # Adaptive hard-stop (dominant-side point move) by entry price
    "hard_stop_pp_under_05": 0.10,     # entries <5% or >95% -> close if dominant side moves +10pp
    "hard_stop_pp_under_10": 0.07,     # entries <10% or >90% -> close if dominant side moves +7pp
    "hard_stop_pp_under_20": 0.07,     # entries <20% or >80% -> close if dominant side moves +7pp
    "hard_stop_pp_standard": 0.10,     # standard: close if dominant side moves +10pp
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
        "min_entry_price": 0.01,          # floor: allow extreme contrarian entries
        "min_edge": 0.10,                 # council Tier 1 — never below 10%
        "min_days_to_expiry_entry": 7,    # Tier 2 — hard block, never disable
        "pre_expiry_lock_days": 5,        # Tier 2 — protect profits, never below 5d
    }
    for key, floor_val in FLOOR.items():
        if merged.get(key, 0) < floor_val:
            merged[key] = floor_val

    # ── Hard overrides: these values always come from code, never from saved file ──
    HARDCODE = {
        "position_size_usdc": 150.0,     # REDUCED — smaller risk per lottery ticket
        "max_days_to_expiry": 60,
        "min_entry_price": 0.01,
        "max_portfolio_risk_pct": 0.20,
        "dd_soft_reduce_pct": 0.05,
        "dd_soft_reduce_factor": 0.75,
        "drawdown_reduce_sizing_pct": 0.10,
        "max_drawdown_pause_pct": 0.15,
        "min_opportunity_score": 50.0,
        "max_positions_per_category": 2,
        # Adaptive TP/SL/Hard-stop by entry price (updated 2026-07-30)
        "tp_under_05": 1.00,
        "tp_under_10": 0.50,
        "tp_under_20": 0.30,
        "tp_standard": 0.20,
        "sl_under_05": 0.0,            # price-based SL off for extreme entries
        "sl_under_10": -0.40,
        "sl_under_20": -0.25,
        "sl_standard": -0.15,
        "catastrophic_stop_pct": 0.50,
        "hard_stop_pp_under_05": 0.10,
        "hard_stop_pp_under_10": 0.07,
        "hard_stop_pp_under_20": 0.07,
        "hard_stop_pp_standard": 0.10,
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

    def _days_to_expiry(self, end_date_str: str) -> Optional[float]:
        """Returns days remaining until market end_date, or None if not set."""
        if not end_date_str:
            return None
        try:
            from dateutil import parser as dateparser
            end_dt = dateparser.parse(end_date_str)
            if end_dt and end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt:
                return (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
        except Exception:
            pass
        return None

    def _score_opportunity(
        self,
        market,
        edge: float,
        news_summary: list[str],
        open_categories: dict[str, int],
    ) -> float:
        """
        Composite score (0-100) for a contrarian opportunity.
        Higher is better. Penalizes low liquidity, short expiry, and
        category concentration.
        """
        score = 0.0

        # 1. Edge (0-35): bigger extreme consensus fade = better
        edge_strength = abs(edge)
        score += min(35.0, max(0.0, (edge_strength - 0.05) / 0.15 * 35.0))

        # 2. Liquidity / market impact (0-25): prefer deep markets
        base_size = self.settings.get("position_size_usdc", 200.0)
        liquidity = getattr(market, "liquidity", 0) or 0
        if liquidity > 0:
            impact_ratio = base_size / liquidity
            # ratio <= 0.05 -> full 25 pts; ratio >= 0.30 -> 0 pts
            score += max(0.0, 25.0 * (1.0 - max(0.0, impact_ratio - 0.05) / 0.25))
        else:
            score += 5.0  # unknown liquidity: conservative

        # 3. Time to expiry (0-15): more time for thesis to play out
        days_left = self._days_to_expiry(getattr(market, "end_date", ""))
        if days_left is None:
            score += 10.0  # open-ended
        else:
            max_days = self.settings.get("max_days_to_expiry", 60)
            score += min(15.0, max(0.0, days_left / max_days * 15.0))

        # 4. Research quality (0-15): fresh news = better informed fade
        score += min(15.0, len(news_summary) * 3.0)

        # 5. Category concentration penalty (0-10) — ignore missing/unknown categories
        category = getattr(market, "category", "") or "unknown"
        cat_count = open_categories.get(category, 0) if category not in ("", "unknown") else 0
        score -= min(10.0, cat_count * 5.0)

        # 6. Fee-free category bonus (+10): geopolitics markets pay 0 taker fee
        if "geopolitics" in category.lower():
            score += 10.0

        return round(max(0.0, min(100.0, score)), 1)

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
                    entry_p = pos.get("entry_price", 0.5)
                    side = pos.get("side", "YES")

                    # ── RISK v4: Adaptive TP/SL by entry price ──
                    # Extreme contrarian entries get wider TP to capture outliers,
                    # while keeping tight SL for capital preservation.
                    if entry_p < 0.05 or entry_p > 0.95:
                        tp = self.settings.get("tp_under_05", 1.00)
                    elif entry_p < 0.10 or entry_p > 0.90:
                        tp = self.settings.get("tp_under_10", 0.50)
                    elif entry_p < 0.20 or entry_p > 0.80:
                        tp = self.settings.get("tp_under_20", 0.30)
                    else:
                        tp = self.settings.get("tp_standard", 0.20)

                    if entry_p < 0.05 or entry_p > 0.95:
                        sl = self.settings.get("sl_under_05", 0.0)
                    elif entry_p < 0.10 or entry_p > 0.90:
                        sl = self.settings.get("sl_under_10", -0.40)
                    elif entry_p < 0.20 or entry_p > 0.80:
                        sl = self.settings.get("sl_under_20", -0.25)
                    else:
                        sl = self.settings.get("sl_standard", -0.15)

                    # Refresh current price from live market data before any decision
                    mkt = mkt_map.get(market_id)
                    close_liquidity = getattr(mkt, 'liquidity', None) if mkt else None
                    live_price = pos.get("current_price", entry_p)
                    exec_mode = self.settings.get("execution_mode", "maker")
                    book = None
                    if mkt:
                        token_id = mkt.yes_token_id if side == "YES" else mkt.no_token_id
                        if token_id:
                            fetched = self.fetcher.get_market_price(token_id)
                            if fetched is not None:
                                live_price = fetched
                                self.trader.update_position_price(market_id, live_price)
                                # Recalculate NET pnl with live price (category-aware taker fee estimate)
                                shares = pos.get("shares", 0)
                                net_value = shares * live_price - taker_fee(shares, live_price, pos.get("category", ""))
                                pnl_pct = (net_value - pos["cost_basis"]) / pos["cost_basis"] if pos["cost_basis"] > 0 else 0
                            # Fetch the order book once per position — used for SL
                            # evaluation (best bid) and passed to close_position
                            try:
                                book = self.fetcher.get_order_book(token_id)
                            except Exception:
                                book = None

                    # Auto-close if market end_date has passed (+ 4h grace)
                    try:
                        end_date_str = pos.get("end_date", "")
                        if end_date_str:
                            from dateutil import parser as dateparser
                            end_dt = dateparser.parse(end_date_str)
                            if end_dt and end_dt.tzinfo is None:
                                end_dt = end_dt.replace(tzinfo=timezone.utc)
                            if end_dt and datetime.now(timezone.utc) > end_dt + timedelta(hours=4):
                                # Try binary payout from the market's final resolution
                                resolution = self.fetcher.get_market_resolution(market_id)
                                if resolution in ("YES", "NO"):
                                    side_outcome = 1.0 if side == resolution else 0.0
                                    log(f"⏰ Resolved {resolution} — binary payout ({side_outcome:.0f}/share): {pos['question'][:50]}")
                                    self.trader.close_position(
                                        market_id, live_price, liquidity=close_liquidity,
                                        execution_mode=exec_mode, book=book,
                                        reason="expired", payout=side_outcome,
                                    )
                                    try:
                                        self.db.resolve_prob_estimates(market_id, side_outcome)
                                    except Exception:
                                        pass
                                    trades_closed += 1
                                    continue
                                # Resolution not available yet (UMA window)
                                days_past = (datetime.now(timezone.utc) - end_dt).days
                                if days_past > 7:
                                    log(f"⏰ Expired {days_past}d ago, still unresolved — closing at last price: {pos['question'][:50]}")
                                    self.trader.close_position(market_id, live_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="expired_unresolved")
                                    trades_closed += 1
                                    continue
                                log(f"⏳ Expired {end_date_str[:10]}, awaiting resolution: {pos['question'][:50]}")
                                continue
                    except Exception:
                        pass

                    # Fallback: zombie close — only for positions with NO end_date.
                    # Positions with a future end_date hold to resolution.
                    try:
                        if not pos.get("end_date", ""):
                            opened_at = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
                            days_open = (datetime.now(timezone.utc) - opened_at).days
                            max_age = self.settings.get("max_position_age_days", 60)
                            if days_open >= max_age:
                                log(f"⏰ Closing zombie position ({days_open}d, no end_date): {pos['question'][:50]}")
                                self.trader.close_position(market_id, live_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="expired")
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
                                    self.trader.close_position(market_id, live_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="pre_expiry_lock")
                                    trades_closed += 1
                                    continue
                    except Exception:
                        pass

                    # ── RISK v4: Hard stop if dominant side moved against us ──
                    # Dynamic hard stop: relaxed for extreme entries to avoid noise,
                    # standard for balanced entries.
                    if entry_p < 0.05 or entry_p > 0.95:
                        hard_stop_pp = self.settings.get("hard_stop_pp_under_05", 0.10)
                    elif entry_p < 0.10 or entry_p > 0.90:
                        hard_stop_pp = self.settings.get("hard_stop_pp_under_10", 0.07)
                    elif entry_p < 0.20 or entry_p > 0.80:
                        hard_stop_pp = self.settings.get("hard_stop_pp_under_20", 0.07)
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
                            self.trader.close_position(market_id, live_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="hard_stop")
                            trades_closed += 1
                            continue

                    # ── RISK v3: Stale close (contrarian-aware; disabled by default) ──
                    # If after N days the market is still at the same extreme (or more),
                    # the contrarian thesis is not working — free the capital.
                    stale_days = self.settings.get("stale_position_days", 0)
                    stale_move = self.settings.get("stale_position_movement", 0.03)
                    try:
                        opened_at = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
                        days_open = (datetime.now(timezone.utc) - opened_at).days
                        if stale_days > 0 and days_open >= stale_days:
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
                                    self.trader.close_position(market_id, live_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="stale")
                                    trades_closed += 1
                                    continue
                                elif side == "NO" and curr_p < entry_p:
                                    # NO went down (market favors YES more) — against us
                                    log(f"💤 Stale+against ({days_open}d, NO fell {movement*100:.1f}%) — cutting: {pos['question'][:50]}")
                                    self.trader.close_position(market_id, live_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="stale")
                                    trades_closed += 1
                                    continue
                                elif movement < stale_move:
                                    # No significant movement at all — free capital
                                    log(f"💤 Stale trade ({days_open}d, {movement*100:.1f}% move) — freeing capital: {pos['question'][:50]}")
                                    self.trader.close_position(market_id, live_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="stale")
                                    trades_closed += 1
                                    continue
                    except Exception:
                        pass

                    if pnl_pct >= tp:
                        log(f"✅ TP hit on {pos['question'][:50]} (+{pnl_pct*100:.1f}%) — closing")
                        try:
                            self.trader.close_position(market_id, live_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="take_profit")
                            trades_closed += 1
                        except Exception as e:
                            log(f"Close error: {e}", "error")
                            errors += 1
                    else:
                        # ── SL re-anchored to best bid (mid fallback) ──
                        sl_price = live_price
                        if book and book.get("bids"):
                            sl_price = book["bids"][0][0]
                        shares = pos.get("shares", 0)
                        net_sl_value = shares * sl_price - taker_fee(shares, sl_price, pos.get("category", ""))
                        pnl_sl = (net_sl_value - pos["cost_basis"]) / pos["cost_basis"] if pos["cost_basis"] > 0 else 0

                        # Catastrophic stop: an extreme entry collapsing to <= 50% of entry
                        # means the market priced in a catalyst — thesis invalidated
                        cat_stop = self.settings.get("catastrophic_stop_pct", 0.50)
                        if (entry_p < 0.05 or entry_p > 0.95) and cat_stop > 0 and sl_price <= entry_p * cat_stop:
                            log(f"🚨 CATALYST STOP: bid {sl_price:.4f} ≤ {cat_stop:.0%}× entry {entry_p:.4f} — {pos['question'][:50]}")
                            try:
                                self.trader.close_position(market_id, sl_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="catalyst_stop")
                                trades_closed += 1
                            except Exception as e:
                                log(f"Close error: {e}", "error")
                                errors += 1
                        elif sl != 0 and pnl_sl <= sl:
                            log(f"🛑 SL hit on {pos['question'][:50]} ({pnl_sl*100:.1f}% @ bid {sl_price:.4f}) — closing")
                            try:
                                self.trader.close_position(market_id, sl_price, liquidity=close_liquidity, execution_mode=exec_mode, book=book, reason="stop_loss")
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
            max_heat = self.settings.get("max_portfolio_risk_pct", 0.20)

            if len(open_pos) >= max_pos:
                log(f"Max positions ({max_pos}) reached — skipping new trades")
            elif balance < self.settings.get("position_size_usdc", 200):
                log(f"Insufficient balance (${balance:.2f}) — skipping new trades")
            elif drawdown >= max_dd_pause:
                log(f"🛑 DRAWDOWN PAUSE: dd={drawdown*100:.1f}% ≥ {max_dd_pause*100:.1f}% — no new entries")
                # also reduce heat: don't compound damage while paused
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

                # Track category concentration for diversification limit
                open_categories: dict[str, int] = {}
                for pos in open_pos.values():
                    cat = pos.get("category", "") or "unknown"
                    open_categories[cat] = open_categories.get(cat, 0) + 1

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
                    # Mercados donde ningún lado supera el ~80% no ofrecen suficiente
                    # edge contrarian. Como YES + NO ≈ 1, basta con comprobar YES.
                    # Esto es simétrico: bloquea YES entre 20% y 80%.
                    if 0.20 < market.yes_price < 0.80:
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

                    if market.yes_price > 0.80 and not disruption:
                        side = "NO"
                        entry_price = market.no_price
                        confidence = "high"
                        edge = market.yes_price - 0.80
                    elif market.no_price > 0.80 and not disruption:
                        side = "YES"
                        entry_price = market.yes_price
                        confidence = "high"
                        edge = market.no_price - 0.80
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

                    # === Composite score + diversification ===
                    category = getattr(market, "category", "") or "unknown"
                    max_per_cat = self.settings.get("max_positions_per_category", 2)
                    if category not in ("", "unknown") and open_categories.get(category, 0) >= max_per_cat:
                        log(f"   🚫 Category '{category}' limit ({max_per_cat}) reached — skip")
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
                            "reason": "category limit",
                        }))
                        continue

                    score = self._score_opportunity(market, edge, news_summary, open_categories)
                    min_score = self.settings.get("min_opportunity_score", 50.0)
                    if score < min_score:
                        log(f"   📉 Opportunity score {score} < {min_score} — skip")
                        logger.info(json.dumps({
                            "event": "CONTRARIAN_EVAL",
                            "market_id": market.id,
                            "question": market.question[:80],
                            "yes_price": round(market.yes_price, 4),
                            "no_price": round(market.no_price, 4),
                            "side": side,
                            "disruption": False,
                            "edge": round(edge, 4),
                            "score": score,
                            "size": 0,
                            "decision": "SKIP",
                            "reason": "low opportunity score",
                        }))
                        continue

                    log(
                        f"   ✅ CONTRARIAN EDGE: {side} | "
                        f"extreme={'YES' if market.yes_price > 0.80 else 'NO'} "
                        f"edge={edge:.3f} score={score} disruption={disruption}"
                    )

                    try:
                        bal = self.db.get_portfolio().get("balance", 0)
                        # === RISK v3: Dynamic sizing using settings multipliers ===
                        base_size = self.settings.get("position_size_usdc", 200.0)
                        edge_strength = abs(edge)
                        conf_key = (confidence or "medium").lower().replace(" ", "_")

                        if edge_strength >= 0.10:
                            mult_key = f"size_multiplier_{conf_key}_conf_large_edge"
                            multiplier = self.settings.get(mult_key, 1.0)
                        elif edge_strength >= 0.05:
                            mult_key = f"size_multiplier_{conf_key}_conf_base"
                            multiplier = self.settings.get(mult_key, 0.75)
                        else:
                            multiplier = 0.5   # $100 (fallback)

                        dynamic_size = round(base_size * multiplier)

                        # Progressive drawdown reduction
                        dd_soft = self.settings.get("dd_soft_reduce_pct", 0.05)
                        dd_soft_factor = self.settings.get("dd_soft_reduce_factor", 0.75)
                        dd_hard = self.settings.get("drawdown_reduce_sizing_pct", 0.10)
                        if drawdown >= dd_hard:
                            dynamic_size = round(dynamic_size * 0.5)
                            log(f"   📉 Sizing halved: drawdown {drawdown*100:.1f}%")
                        elif drawdown >= dd_soft:
                            dynamic_size = round(dynamic_size * dd_soft_factor)
                            log(f"   📉 Sizing reduced 25%: drawdown {drawdown*100:.1f}%")

                        # CAP: max 5% of total portfolio value per position
                        portfolio_stats = self.db.get_stats()
                        portfolio_value = portfolio_stats.get("total_value", 10000)
                        max_position = portfolio_value * 0.05
                        dynamic_size = min(dynamic_size, max_position)

                        # FLOOR: minimum $100
                        dynamic_size = max(dynamic_size, 100.0)

                        size = min(dynamic_size, bal)
                        exec_mode = self.settings.get("execution_mode", "maker")
                        open_book = None
                        open_token = market.yes_token_id if side == "YES" else market.no_token_id
                        if open_token:
                            try:
                                open_book = self.fetcher.get_order_book(open_token)
                            except Exception:
                                open_book = None
                        self.trader.open_position(
                            market_id=market.id,
                            question=market.question,
                            side=side,
                            entry_price=entry_price,
                            amount_usdc=size,
                            estimated_prob=0.5,  # no longer used meaningfully
                            confidence=confidence,
                            reasoning=f"Extreme contrarian: fade {'YES' if market.yes_price > 0.80 else 'NO'} consensus @ {edge:.1%} excess. News: {'; '.join(news_summary[:3])}",
                            end_date=market.end_date,
                            liquidity=market.liquidity,
                            category=market.category,
                            execution_mode=exec_mode,
                            book=open_book,
                            settings_version=SETTINGS_VERSION,
                            entry_bucket=entry_bucket(entry_price),
                        )
                        trades_opened += 1
                        # Record the probability estimate for calibration
                        # (Brier score filled in when the market resolves)
                        try:
                            model_name = getattr(getattr(self.researcher, "llm", None), "model", "") or ""
                            self.db.add_prob_estimate({
                                "market_id": market.id,
                                "question": market.question,
                                "side": side,
                                "estimated_prob": round(min(entry_price + edge, 0.99), 4),
                                "confidence": confidence,
                                "edge": round(edge, 4),
                                "market_price": round(entry_price, 4),
                                "as_of": datetime.now(timezone.utc).isoformat(),
                                "model": model_name,
                                "prompt_version": "contrarian-v2",
                                "settings_version": SETTINGS_VERSION,
                            })
                        except Exception as e:
                            log(f"   prob_estimate write failed: {e}", "warning")
                        # Update in-memory open_pos and category count to prevent re-entry in same cycle
                        open_pos[market.id] = {"entry_price": entry_price, "category": market.category}
                        open_categories[category] = open_categories.get(category, 0) + 1
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
                            "score": score,
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

    # Live portfolio health metrics
    try:
        db = PortfolioDatabase()
        stats = db.get_stats()
        equity = db.get_equity_history()
        portfolio = db.get_portfolio()

        peak = max((e.get("value", 0) for e in equity), default=stats.get("total_value", 0))
        current = stats.get("total_value", 0)
        drawdown = (peak - current) / peak if peak > 0 else 0.0

        positions = portfolio.get("positions", {})
        heat = (
            sum(p.get("cost_basis", 0) for p in positions.values()) / current
            if current > 0 else 0.0
        )

        alerts = []
        if drawdown >= settings.get("max_drawdown_pause_pct", 0.20):
            alerts.append({"level": "critical", "msg": f"Drawdown pause active: {drawdown*100:.1f}%"})
        elif drawdown >= settings.get("drawdown_reduce_sizing_pct", 0.10):
            alerts.append({"level": "warning", "msg": f"Drawdown elevated: {drawdown*100:.1f}%"})
        if heat >= settings.get("max_portfolio_risk_pct", 0.20):
            alerts.append({"level": "warning", "msg": f"Heat limit reached: {heat*100:.1f}%"})
        if stats.get("win_rate", 0) < 30 and stats.get("closed_trades", 0) >= 10:
            alerts.append({"level": "warning", "msg": f"Win rate low: {stats.get('win_rate', 0)}%"})
        if stats.get("open_positions", 0) >= settings.get("max_open_positions", 5):
            alerts.append({"level": "info", "msg": "Max open positions reached"})

        health = {
            "stats": stats,
            "drawdown": round(drawdown, 4),
            "heat": round(heat, 4),
            "peak_equity": round(peak, 2),
            "alerts": alerts,
        }
    except Exception as e:
        health = {"error": str(e)}

    return {
        "running": running,
        "settings": settings,
        "last_run": (get_runs(1) or [None])[0],
        "health": health,
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
