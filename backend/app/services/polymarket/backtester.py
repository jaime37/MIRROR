"""
Backtester for the MiroFish contrarian strategy on Polymarket.

Fetches historical YES-token prices from the CLOB prices-history endpoint and
simulates fading the consensus when one side exceeds 80 %. The simulation
includes fees and optional slippage using the same engine logic as the paper
trader.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from .market_fetcher import Market, MarketFetcher
from .paper_trader import FEE_RATE, _apply_slippage


CLOB_API = "https://clob.polymarket.com"


@dataclass
class SimulatedTrade:
    market_id: str
    question: str
    side: str
    entry_ts: int
    entry_mid: float
    entry_fill: float
    exit_ts: int
    exit_mid: float
    exit_fill: float
    amount: float
    pnl: float
    pnl_pct: float
    reason: str


class Backtester:
    def __init__(self, settings: Optional[dict] = None, fidelity_minutes: int = 720):
        self.settings = settings or {}
        self.fidelity_minutes = fidelity_minutes
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    # ── Data fetching ───────────────────────────────────────────────────────────

    def fetch_price_history(self, token_id: str) -> list[dict]:
        """Returns list of {t, p} timestamps (seconds) and prices for a token."""
        url = f"{CLOB_API}/prices-history"
        params = {
            "market": token_id,
            "interval": "max",
            "fidelity": self.fidelity_minutes,
        }
        try:
            resp = self.session.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json().get("history", [])
        except Exception as e:
            raise RuntimeError(f"Failed to fetch price history for {token_id}: {e}")

    # ── Simulation ──────────────────────────────────────────────────────────────

    def _tp_sl(self, entry_price: float) -> tuple[float, float]:
        """Returns (tp, sl) net-return thresholds for a given entry price."""
        if entry_price < 0.05 or entry_price > 0.95:
            tp = self.settings.get("tp_under_05", 1.00)
            sl = self.settings.get("sl_under_05", -0.15)
        elif entry_price < 0.10 or entry_price > 0.90:
            tp = self.settings.get("tp_under_10", 0.50)
            sl = self.settings.get("sl_under_10", -0.10)
        elif entry_price < 0.20 or entry_price > 0.80:
            tp = self.settings.get("tp_under_20", 0.30)
            sl = self.settings.get("sl_under_20", -0.15)
        else:
            tp = self.settings.get("tp_standard", 0.20)
            sl = self.settings.get("sl_standard", -0.15)
        return tp, sl

    def _hard_stop_pp(self, entry_price: float) -> float:
        if entry_price < 0.05 or entry_price > 0.95:
            return self.settings.get("hard_stop_pp_under_05", 0.10)
        elif entry_price < 0.10 or entry_price > 0.90:
            return self.settings.get("hard_stop_pp_under_10", 0.07)
        elif entry_price < 0.20 or entry_price > 0.80:
            return self.settings.get("hard_stop_pp_under_20", 0.07)
        return self.settings.get("hard_stop_pp_standard", 0.10)

    def _net_pnl(
        self,
        amount: float,
        entry_mid: float,
        exit_mid: float,
        liquidity: float,
    ) -> tuple[float, float, float, float]:
        """Returns (entry_fill, exit_fill, pnl, pnl_pct)."""
        entry_fill = _apply_slippage(entry_mid, amount, liquidity, action="buy")
        shares = (amount * (1 - FEE_RATE)) / entry_fill if entry_fill > 0 else 0
        notional = shares * exit_mid
        exit_fill = _apply_slippage(exit_mid, notional, liquidity, action="sell")
        proceeds = shares * exit_fill * (1 - FEE_RATE)
        pnl = proceeds - amount
        return entry_fill, exit_fill, pnl, (pnl / amount if amount > 0 else 0)

    def simulate_market(
        self,
        market: Market,
        allow_reentry: bool = False,
    ) -> list[SimulatedTrade]:
        """
        Simulates the contrarian strategy on a single market's historical YES
        price series. Returns a list of completed simulated trades.
        If allow_reentry is True, the strategy can re-enter the same market
        after a close (useful for capital-aware portfolio backtests).
        """
        if not market.yes_token_id:
            return []

        history = self.fetch_price_history(market.yes_token_id)
        if len(history) < 2:
            return []

        min_edge = self.settings.get("min_edge", 0.10)
        amount = self.settings.get("position_size_usdc", 200.0)
        end_ts = None
        if market.end_date:
            try:
                end_dt = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                end_ts = int(end_dt.timestamp())
            except Exception:
                end_ts = None

        trades: list[SimulatedTrade] = []
        position: Optional[dict] = None
        has_traded = False

        for i, point in enumerate(history):
            ts = int(point.get("t", 0))
            yes_price = float(point.get("p", 0))
            no_price = 1.0 - yes_price

            # Expiry close
            if position and end_ts and ts > end_ts + 4 * 3600:
                entry_mid = position["entry_yes"]
                exit_mid = yes_price if position["side"] == "NO" else no_price
                entry_fill, exit_fill, pnl, pnl_pct = self._net_pnl(
                    amount, entry_mid, exit_mid, market.liquidity
                )
                trades.append(
                    SimulatedTrade(
                        market_id=market.id,
                        question=market.question,
                        side=position["side"],
                        entry_ts=position["entry_ts"],
                        entry_mid=entry_mid,
                        entry_fill=entry_fill,
                        exit_ts=ts,
                        exit_mid=exit_mid,
                        exit_fill=exit_fill,
                        amount=amount,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        reason="expired",
                    )
                )
                position = None
                if not allow_reentry:
                    has_traded = True
                continue

            if position is None and not has_traded:
                side = None
                entry_mid = None
                edge = 0.0
                if yes_price > 0.80 and (yes_price - 0.80) >= min_edge:
                    side = "NO"
                    entry_mid = no_price
                    edge = yes_price - 0.80
                elif no_price > 0.80 and (no_price - 0.80) >= min_edge:
                    side = "YES"
                    entry_mid = yes_price
                    edge = no_price - 0.80

                if side and entry_mid and entry_mid >= self.settings.get("min_entry_price", 0.03):
                    position = {
                        "side": side,
                        "entry_ts": ts,
                        "entry_yes": yes_price,
                        "entry_mid": entry_mid,
                        "tp": self._tp_sl(entry_mid)[0],
                        "sl": self._tp_sl(entry_mid)[1],
                        "hard_stop_pp": self._hard_stop_pp(entry_mid),
                    }
                continue

            if position is None:
                continue

            # Update current price for the held side
            current_mid = no_price if position["side"] == "NO" else yes_price
            pnl_pct = (current_mid - position["entry_mid"]) / position["entry_mid"]

            # Hard stop: dominant side moved further against us
            reason = None
            if position["side"] == "NO":
                dominant_move = yes_price - position["entry_yes"]
                if dominant_move >= position["hard_stop_pp"]:
                    reason = "hard_stop"
            else:
                # we bought YES; dominant NO went from (1 - entry_yes) to no_price
                dominant_at_entry = 1.0 - position["entry_yes"]
                dominant_move = no_price - dominant_at_entry
                if dominant_move >= position["hard_stop_pp"]:
                    reason = "hard_stop"

            if pnl_pct >= position["tp"]:
                reason = "take_profit"
            elif pnl_pct <= position["sl"]:
                reason = "stop_loss"

            if reason:
                entry_fill, exit_fill, pnl, pnl_pct_net = self._net_pnl(
                    amount, position["entry_mid"], current_mid, market.liquidity
                )
                trades.append(
                    SimulatedTrade(
                        market_id=market.id,
                        question=market.question,
                        side=position["side"],
                        entry_ts=position["entry_ts"],
                        entry_mid=position["entry_mid"],
                        entry_fill=entry_fill,
                        exit_ts=ts,
                        exit_mid=current_mid,
                        exit_fill=exit_fill,
                        amount=amount,
                        pnl=pnl,
                        pnl_pct=pnl_pct_net,
                        reason=reason,
                    )
                )
                position = None
                if not allow_reentry:
                    has_traded = True

        return trades

    # ── Aggregations ────────────────────────────────────────────────────────────

    def run(self, limit: int = 20, min_volume: float = 5000, min_liquidity: float = 1000) -> dict:
        """Runs the backtest over the top active markets and returns metrics."""
        fetcher = MarketFetcher()
        markets = fetcher.get_active_markets(
            limit=limit,
            min_volume=min_volume,
            min_liquidity=min_liquidity,
        )

        all_trades: list[SimulatedTrade] = []
        for market in markets:
            try:
                trades = self.simulate_market(market)
                all_trades.extend(trades)
            except Exception as e:
                # Skip markets where history is unavailable or malformed
                continue

        if not all_trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "max_drawdown": 0.0,
                "markets_tested": len(markets),
            }

        wins = [t for t in all_trades if t.pnl > 0]
        losses = [t for t in all_trades if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in all_trades)

        # Cumulative P&L drawdown
        peak = 0.0
        equity = 0.0
        max_dd = 0.0
        for t in sorted(all_trades, key=lambda x: x.exit_ts):
            equity += t.pnl
            peak = max(peak, equity)
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd

        return {
            "markets_tested": len(markets),
            "total_trades": len(all_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(all_trades) if all_trades else 0.0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(all_trades), 2) if all_trades else 0.0,
            "avg_win": round(sum(t.pnl for t in wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(t.pnl for t in losses) / len(losses), 2) if losses else 0.0,
            "max_drawdown": round(max_dd, 2),
        }

    def run_portfolio(
        self,
        limit: int = 20,
        min_volume: float = 5000,
        min_liquidity: float = 1000,
        initial_balance: float = 10_000.0,
    ) -> dict:
        """
        Capital-aware backtest. Only enters trades when there is enough cash
        and free position slots, respecting max_open_positions.
        """
        fetcher = MarketFetcher()
        markets = fetcher.get_active_markets(
            limit=limit,
            min_volume=min_volume,
            min_liquidity=min_liquidity,
        )

        max_open = self.settings.get("max_open_positions", 5)
        amount = self.settings.get("position_size_usdc", 200.0)

        # Build all candidate trades per market (allow re-entry)
        candidate_trades: list[SimulatedTrade] = []
        for market in markets:
            try:
                candidate_trades.extend(self.simulate_market(market, allow_reentry=True))
            except Exception:
                continue

        if not candidate_trades:
            return {
                "markets_tested": len(markets),
                "total_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "max_drawdown": 0.0,
                "final_equity": initial_balance,
                "capital_limited_skips": 0,
            }

        # Event-driven simulation: exits processed before entries at same timestamp
        events = []
        for t in candidate_trades:
            events.append((t.exit_ts, 0, "exit", t))
            events.append((t.entry_ts, 1, "enter", t))
        events.sort(key=lambda x: (x[0], x[1]))

        cash = float(initial_balance)
        equity = cash
        peak = equity
        max_dd = 0.0
        accepted_trades: list[SimulatedTrade] = []
        open_markets: set[str] = set()
        open_count = 0
        capital_limited_skips = 0
        equity_history = [{"timestamp": events[0][0] - 1, "equity": round(equity, 2)}]

        for _ts, _order, kind, trade in events:
            if kind == "exit":
                if trade.market_id in open_markets:
                    cash += trade.amount + trade.pnl
                    open_markets.remove(trade.market_id)
                    open_count -= 1
                    equity = cash  # assumes positions marked at close
                    peak = max(peak, equity)
                    dd = peak - equity
                    if dd > max_dd:
                        max_dd = dd
                    equity_history.append({"timestamp": _ts, "equity": round(equity, 2)})
            else:  # enter
                if trade.market_id in open_markets:
                    continue
                if open_count >= max_open:
                    capital_limited_skips += 1
                    continue
                if cash < amount:
                    capital_limited_skips += 1
                    continue
                cash -= amount
                open_markets.add(trade.market_id)
                open_count += 1
                accepted_trades.append(trade)

        wins = [t for t in accepted_trades if t.pnl > 0]
        losses = [t for t in accepted_trades if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in accepted_trades)

        return {
            "markets_tested": len(markets),
            "candidate_trades": len(candidate_trades),
            "accepted_trades": len(accepted_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(accepted_trades) if accepted_trades else 0.0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(accepted_trades), 2) if accepted_trades else 0.0,
            "avg_win": round(sum(t.pnl for t in wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(t.pnl for t in losses) / len(losses), 2) if losses else 0.0,
            "max_drawdown": round(max_dd, 2),
            "final_equity": round(equity, 2),
            "capital_limited_skips": capital_limited_skips,
            "equity_history": equity_history[-1000:],
        }
