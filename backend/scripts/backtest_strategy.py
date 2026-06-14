#!/usr/bin/env python3
"""
Run a historical backtest of the MiroFish contrarian strategy.

Usage:
    cd backend
    .venv/Scripts/python scripts/backtest_strategy.py [--limit N] [--fidelity 720]
"""

import argparse
import json
import sys
import os

# Ensure backend root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.polymarket.autonomous_pipeline import load_settings
from app.services.polymarket.backtester import Backtester


def main():
    parser = argparse.ArgumentParser(description="Backtest MiroFish contrarian strategy")
    parser.add_argument("--limit", type=int, default=30, help="Number of top markets to test")
    parser.add_argument("--fidelity", type=int, default=720, help="Candle fidelity in minutes (default 12h)")
    parser.add_argument("--min-volume", type=float, default=5000, help="Minimum market volume")
    parser.add_argument("--min-liquidity", type=float, default=1000, help="Minimum market liquidity")
    args = parser.parse_args()

    settings = load_settings()
    backtester = Backtester(settings=settings, fidelity_minutes=args.fidelity)

    print(f"Running backtest on up to {args.limit} markets (fidelity={args.fidelity}m)...")
    results = backtester.run(
        limit=args.limit,
        min_volume=args.min_volume,
        min_liquidity=args.min_liquidity,
    )

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
