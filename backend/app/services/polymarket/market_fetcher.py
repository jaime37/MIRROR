"""
Fetches active prediction markets from Polymarket's Gamma API.
No authentication required for read operations.
"""

import json
import requests
from typing import Optional
from dataclasses import dataclass


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"


@dataclass
class Market:
    id: str
    condition_id: str
    question: str
    description: str
    end_date: str
    category: str
    volume: float
    liquidity: float
    yes_token_id: Optional[str]
    no_token_id: Optional[str]
    yes_price: float
    no_price: float
    active: bool
    closed: bool
    enable_order_book: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "condition_id": self.condition_id,
            "question": self.question,
            "description": self.description,
            "end_date": self.end_date,
            "category": self.category,
            "volume": self.volume,
            "liquidity": self.liquidity,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "active": self.active,
            "closed": self.closed,
            "enable_order_book": self.enable_order_book,
        }


class MarketFetcher:
    def __init__(self, timeout: int = 15):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.timeout = timeout

    def get_active_markets(
        self,
        limit: int = 50,
        min_volume: float = 1000,
        min_liquidity: float = 500,
        category: Optional[str] = None,
    ) -> list[Market]:
        """
        Returns active markets sorted by volume descending.
        Pulls markets from top events (by event volume) so we get
        the most liquid and relevant markets, not obscure ones.
        """
        # First fetch top events sorted by volume
        event_params = {
            "active": "true",
            "closed": "false",
            "limit": 50,
            "order": "volume",
            "ascending": "false",
        }
        try:
            ev_resp = self.session.get(f"{GAMMA_API}/events", params=event_params, timeout=self.timeout)
            ev_resp.raise_for_status()
            events = ev_resp.json()
            # Collect market IDs from top events
            top_market_ids = set()
            for ev in events:
                for mkt in (ev.get("markets") or []):
                    top_market_ids.add(str(mkt.get("id", "")))
        except Exception:
            top_market_ids = set()

        # Now fetch markets
        params = {
            "active": "true",
            "closed": "false",
            "enableOrderBook": "true",
            "limit": min(limit * 5, 300),
            "order": "volume",
            "ascending": "false",
        }
        if category:
            params["category"] = category

        resp = self.session.get(f"{GAMMA_API}/markets", params=params, timeout=self.timeout)
        resp.raise_for_status()
        raw = resp.json()

        # Prioritise markets that belong to top events
        if top_market_ids:
            raw = sorted(raw, key=lambda m: (str(m.get("id","")) not in top_market_ids, -float(m.get("volume", 0) or 0)))

        markets = []
        for m in raw:
            volume    = float(m.get("volume", 0) or 0)
            liquidity = float(m.get("liquidity", 0) or 0)
            if volume < min_volume or liquidity < min_liquidity:
                continue

            # ── Token IDs ─────────────────────────────────────────────────────
            # The Gamma API returns clobTokenIds as a JSON-encoded string array
            clob_raw = m.get("clobTokenIds") or "[]"
            try:
                token_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
            except Exception:
                token_ids = []
            yes_token_id = token_ids[0] if len(token_ids) > 0 else None
            no_token_id  = token_ids[1] if len(token_ids) > 1 else None

            # ── Prices ────────────────────────────────────────────────────────
            # outcomePrices is a JSON string like ["0.014", "0.986"]
            prices_raw = m.get("outcomePrices") or "[]"
            try:
                outcome_prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            except Exception:
                outcome_prices = []

            if len(outcome_prices) >= 2:
                yes_price = float(outcome_prices[0])
                no_price  = float(outcome_prices[1])
            else:
                # fallback: use lastTradePrice if available
                ltp = m.get("lastTradePrice")
                yes_price = float(ltp) if ltp else 0.5
                no_price  = round(1.0 - yes_price, 4)

            markets.append(Market(
                id=str(m.get("id", "")),
                condition_id=str(m.get("conditionId", "")),
                question=m.get("question", ""),
                description=m.get("description", ""),
                end_date=m.get("endDate", ""),
                category=m.get("category", ""),
                volume=volume,
                liquidity=liquidity,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                yes_price=yes_price,
                no_price=no_price,
                active=bool(m.get("active", True)),
                closed=bool(m.get("closed", False)),
                enable_order_book=bool(m.get("enableOrderBook", False)),
            ))

            if len(markets) >= limit:
                break

        return markets

    def get_market_price(self, token_id: str) -> Optional[float]:
        """Fetches current midpoint price for a token via CLOB API."""
        try:
            resp = self.session.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": token_id},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return float(resp.json().get("mid", 0))
        except Exception:
            return None

    def refresh_prices(self, market: Market) -> Market:
        """Returns the market with updated live prices from CLOB."""
        if market.yes_token_id:
            price = self.get_market_price(market.yes_token_id)
            if price is not None:
                market.yes_price = round(price, 4)
                market.no_price  = round(1.0 - price, 4)
        return market
