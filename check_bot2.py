#!/usr/bin/env python3
import json, urllib.request, datetime

API = "https://mirror-production-bff6.up.railway.app/api/polymarket"

def fetch(path):
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

print("=" * 60)
print("BOT DEEP DIVE")
print("=" * 60)

# Portfolio discrepancy
print("\n--- 1. PORTFOLIO DISCREPANCY ---")
data = fetch("/portfolio")
d = data.get("data", {})
portfolio = d.get("portfolio", {})
positions = portfolio.get("positions", {})
stats = d.get("stats", {})
print(f"Stats says open_positions: {stats.get('open_positions')}")
print(f"Actual positions dict len: {len(positions)}")
print(f"Positions keys: {list(positions.keys())}")

# Check equity history
print("\n--- 2. EQUITY HISTORY (last 5) ---")
for e in d.get("equity_history", [])[-5:]:
    print(f"  {e.get('timestamp')}: ${e.get('value'):,.4f}")

# Bot runs
print("\n--- 3. BOT RUNS (last 5) ---")
try:
    data = fetch("/bot/runs?limit=5")
    for run in data.get("data", []):
        print(f"  Run {run.get('run_id')} | {run.get('started_at')} | "
              f"Markets: {run.get('markets_scanned')} | "
              f"Opened: {run.get('trades_opened')} | Closed: {run.get('trades_closed')} | "
              f"Errors: {run.get('errors')} | PnL: ${run.get('total_pnl', 0):,.4f}")
except Exception as e:
    print(f"Failed: {e}")

# Last 5 OPEN trades
print("\n--- 4. LAST 5 OPEN TRADES ---")
data = fetch("/trades?limit=50")
open_trades = [t for t in data.get("data", []) if t.get("type") == "OPEN"][:5]
for t in open_trades:
    print(f"  {t.get('timestamp')} | {t.get('question', '?')[:50]}... | ${t.get('amount_usdc')} @ {t.get('price')} | {t.get('side')}")

# Last 10 CLOSE trades
print("\n--- 5. LAST 10 CLOSE TRADES ---")
close_trades = [t for t in data.get("data", []) if t.get("type") == "CLOSE"][:10]
for t in close_trades:
    pnl = t.get("pnl", 0)
    icon = "[WIN]" if pnl > 0 else "[LOSS]"
    print(f"  {icon} {t.get('timestamp')} | {t.get('question', '?')[:45]}... | ${pnl:+.2f} ({t.get('pnl_pct'):.1f}%) | {t.get('reason')}")

print("\n" + "=" * 60)
print("DONE")
print("=" * 60)
