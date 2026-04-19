"""
Orchestrates the full MiroFish → Polymarket paper trading pipeline:
  1. Fetch active markets
  2. For each market, check if we have a MiroFish report with a relevant signal
  3. If edge found → open paper position
  4. Update prices of existing open positions
"""

from typing import Optional
from .market_fetcher import MarketFetcher, Market
from .signal_extractor import SignalExtractor
from .paper_trader import PaperTrader
from .portfolio_db import PortfolioDatabase
from ...utils.logger import get_logger

logger = get_logger("mirofish.polymarket.pipeline")

DEFAULT_POSITION_SIZE_USDC = 200.0  # virtual USDC per trade
MIN_EDGE = 0.06                      # minimum probability edge to trade
MIN_CONFIDENCE = {"high", "medium"}  # only trade on medium+ confidence signals


class PolymarketPipeline:
    def __init__(
        self,
        position_size: float = DEFAULT_POSITION_SIZE_USDC,
        min_edge: float = MIN_EDGE,
    ):
        self.fetcher = MarketFetcher()
        self.extractor = SignalExtractor()
        self.trader = PaperTrader()
        self.db = PortfolioDatabase()
        self.position_size = position_size
        self.min_edge = min_edge

    def run_from_report(
        self,
        report_markdown: str,
        simulation_id: Optional[str] = None,
        report_id: Optional[str] = None,
        max_markets: int = 20,
        dry_run: bool = False,
    ) -> dict:
        """
        Given a MiroFish report, scan Polymarket markets for trading opportunities.

        Returns a summary dict with matches found and trades placed.
        """
        logger.info("Pipeline started: fetching markets")
        markets = self.fetcher.get_active_markets(limit=max_markets)
        logger.info(f"Fetched {len(markets)} active markets")

        results = []
        trades_placed = 0
        portfolio = self.db.get_portfolio()

        for market in markets:
            # skip if position already open
            if market.id in portfolio.get("positions", {}):
                continue

            signal = self.extractor.extract(market.question, report_markdown)
            prob = signal.get("probability")
            confidence = signal.get("confidence", "irrelevant")
            relevant = signal.get("relevant", False)

            result = {
                "market_id": market.id,
                "question": market.question,
                "yes_price": market.yes_price,
                "volume": market.volume,
                "signal": signal,
                "action": "skip",
                "trade": None,
            }

            if not relevant or prob is None or confidence not in MIN_CONFIDENCE:
                results.append(result)
                continue

            edge_info = self.extractor.has_edge(prob, market.yes_price, self.min_edge)

            if not edge_info["has_edge"]:
                result["action"] = "no_edge"
                result["edge_info"] = edge_info
                results.append(result)
                continue

            side = edge_info["side"]
            entry_price = market.yes_price if side == "YES" else market.no_price

            result["action"] = "trade" if not dry_run else "dry_run"
            result["edge_info"] = edge_info

            if not dry_run and portfolio["balance"] >= self.position_size:
                try:
                    trade = self.trader.open_position(
                        market_id=market.id,
                        question=market.question,
                        side=side,
                        entry_price=entry_price,
                        amount_usdc=self.position_size,
                        estimated_prob=prob,
                        confidence=confidence,
                        reasoning=signal.get("reasoning", ""),
                        simulation_id=simulation_id,
                        report_id=report_id,
                    )
                    result["trade"] = trade
                    trades_placed += 1
                    # refresh portfolio balance for next iteration
                    portfolio = self.db.get_portfolio()
                except Exception as e:
                    result["action"] = "error"
                    result["error"] = str(e)
                    logger.error(f"Trade failed for market {market.id}: {e}")

            results.append(result)

        return {
            "markets_scanned": len(markets),
            "opportunities_found": sum(1 for r in results if r.get("edge_info", {}).get("has_edge")),
            "trades_placed": trades_placed,
            "dry_run": dry_run,
            "results": results,
        }

    def refresh_positions(self) -> dict:
        """
        Fetches latest prices for all open positions and updates unrealized P&L.
        Returns updated positions dict.
        """
        portfolio = self.db.get_portfolio()
        positions = portfolio.get("positions", {})
        updated = 0

        for market_id, pos in positions.items():
            token_id = None
            # try to get token id from stored data; fallback to fetching from API
            try:
                markets = self.fetcher.get_active_markets(limit=100)
                market = next((m for m in markets if m.id == market_id), None)
                if market:
                    token_id = market.yes_token_id if pos["side"] == "YES" else market.no_token_id
            except Exception:
                pass

            if token_id:
                price = self.fetcher.get_market_price(token_id)
                if price is not None:
                    self.trader.update_position_price(market_id, price)
                    updated += 1

        return {"updated_positions": updated, "total_positions": len(positions)}
