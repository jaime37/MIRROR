#!/usr/bin/env python3
import json, sys, urllib.request, datetime

API = "https://mirror-production-bff6.up.railway.app/api/polymarket"

def fetch(path):
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

print("=" * 60)
print("BOT STATUS CHECK")
print("=" * 60)

# 1. Bot Status
print("\n--- 1. BOT STATUS ---")
try:
    data = fetch("/bot/status")
    d = data.get("data", {})
    print(f"Running: {d.get('running', False)}")
    last = d.get("last_run", {})
    print(f"Last Run ID: {last.get('run_id', 'N/A')}")
    print(f"Started: {last.get('started_at', 'N/A')}")
    print(f"Finished: {last.get('finished_at', 'N/A')}")
    print(f"Portfolio Value: ${last.get('portfolio_value', 0):,.4f}")
    print(f"Total PnL: ${last.get('total_pnl', 0):,.4f}")
    print(f"Open Positions: {last.get('open_positions', 0)}")
    print(f"Markets Scanned: {last.get('markets_scanned', 0)}")
    print(f"Trades Opened: {last.get('trades_opened', 0)}")
    print(f"Trades Closed: {last.get('trades_closed', 0)}")
    print(f"Errors: {last.get('errors', 0)}")
    
    settings = d.get("settings", {})
    print(f"\nCycle Interval: {settings.get('cycle_interval_minutes', 0)} min")
    print(f"Max Open Positions: {settings.get('max_open_positions', 0)}")
    print(f"Position Size: ${settings.get('position_size_usdc', 0)}")
    print(f"Min Confidence: {settings.get('min_confidence', [])}")
    print(f"Min Edge: {settings.get('min_edge', 0)}")
    print(f"Take Profit: {settings.get('take_profit', 0)*100}%")
    print(f"Stop Loss: {settings.get('stop_loss', 0)*100}%")
    print(f"Max Markets/Cycle: {settings.get('max_markets_per_cycle', 0)}")
    
    logs = last.get("log", [])
    warnings = [l for l in logs if l.get("level") == "warning"]
    errors = [l for l in logs if l.get("level") == "error"]
    if warnings:
        print(f"\n[WARNING] {len(warnings)} warnings in last run:")
        for w in warnings:
            print(f"  [{w.get('ts', '?')}] {w.get('msg', '')}")
    if errors:
        print(f"\n[ERROR] {len(errors)} errors in last run:")
        for e in errors:
            print(f"  [{e.get('ts', '?')}] {e.get('msg', '')}")
    if not warnings and not errors:
        print("\n[OK] No warnings or errors in last run")
except Exception as e:
    print(f"Failed: {e}")

# 2. Portfolio
print("\n--- 2. PORTFOLIO ---")
try:
    data = fetch("/portfolio")
    d = data.get("data", {})
    stats = d.get("stats", {})
    print(f"Balance: ${stats.get('balance', 0):,.4f}")
    print(f"Positions Value: ${stats.get('positions_value', 0):,.4f}")
    print(f"Total Value: ${stats.get('total_value', 0):,.4f}")
    print(f"Total PnL: ${stats.get('total_pnl', 0):,.4f}")
    print(f"Realized PnL: ${stats.get('realized_pnl', 0):,.4f}")
    print(f"Unrealized PnL: ${stats.get('unrealized_pnl', 0):,.4f}")
    print(f"Total Return: {stats.get('total_return_pct', 0):,.2f}%")
    print(f"Total Trades: {stats.get('total_trades', 0)}")
    print(f"Closed Trades: {stats.get('closed_trades', 0)}")
    print(f"Open Positions: {stats.get('open_positions', 0)}")
    print(f"Win Rate: {stats.get('win_rate', 0):,.1f}%")
    print(f"Wins: {stats.get('wins', 0)}")
    print(f"Losses: {stats.get('losses', 0)}")
    
    portfolio = d.get("portfolio", {})
    positions = portfolio.get("positions", {})
    print(f"\n--- OPEN POSITIONS ({len(positions)}) ---")
    for pid, pos in positions.items():
        print(f"\n>> {pos.get('question', 'Unknown')[:65]}...")
        print(f"   Market ID: {pid}")
        print(f"   Side: {pos.get('side', '?')}")
        print(f"   Entry Price: {pos.get('entry_price', 0):.4f}")
        print(f"   Current Price: {pos.get('current_price', 0):.4f}")
        print(f"   Shares: {pos.get('shares', 0):,.4f}")
        print(f"   Value: ${pos.get('value', 0):,.4f}")
        print(f"   Unrealized PnL: ${pos.get('unrealized_pnl', 0):,.4f}")
        print(f"   Unrealized PnL %: {pos.get('unrealized_pnl_pct', 0):,.2f}%")
        print(f"   Invested: ${pos.get('invested', 0):,.4f}")
        print(f"   Opened: {pos.get('opened_at', '?')}")
        try:
            opened = datetime.datetime.fromisoformat(pos.get('opened_at', '').replace('Z', '+00:00'))
            days = (datetime.datetime.now(datetime.timezone.utc) - opened).days
            print(f"   Days Open: {days}")
        except:
            pass
        if pos.get('days_to_expiry') is not None:
            print(f"   Days to Expiry: {pos.get('days_to_expiry')}")
        if pos.get('take_profit'):
            print(f"   TP Level: {pos.get('take_profit'):.4f}")
        if pos.get('stop_loss'):
            print(f"   SL Level: {pos.get('stop_loss'):.4f}")
except Exception as e:
    print(f"Failed: {e}")

# 3. Recent Trades
print("\n--- 3. RECENT TRADES (last 15) ---")
try:
    data = fetch("/trades?limit=15")
    trades = data.get("data", [])
    for t in trades:
        ts = t.get('timestamp', '?')
        ttype = t.get('type', '?')
        if ttype == 'CLOSE':
            pnl = t.get('pnl', 0)
            pnl_pct = t.get('pnl_pct', 0)
            reason = t.get('reason', '?')
            icon = "[+WIN]" if pnl > 0 else "[-LOSS]"
            print(f"{icon} [{ts}] CLOSE: {t.get('question', '')[:55]}... | PnL: ${pnl:,.2f} ({pnl_pct:,.1f}%) | {reason}")
        else:
            amt = t.get('amount_usdc', 0)
            conf = t.get('confidence', '?')
            price = t.get('price', 0)
            print(f"[OPEN] [{ts}] OPEN: {t.get('question', '')[:55]}... | ${amt} @ {price:.4f} | {conf}")
except Exception as e:
    print(f"Failed: {e}")

print("\n" + "=" * 60)
print("CHECK COMPLETE")
print("=" * 60)
