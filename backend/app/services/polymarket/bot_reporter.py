"""
Automatic reporting system for the MiroFish Polymarket bot.
Generates structured reports every N cycles and stores them in Railway Volume.
"""

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional


class BotReporter:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.reports_dir = os.path.join(data_dir, "reports")
        os.makedirs(self.reports_dir, exist_ok=True)
        self.last_report_time = None

    def _load_json(self, filename: str) -> Optional[Dict]:
        path = os.path.join(self.data_dir, filename)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def generate_report(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        # Use SQLite-backed portfolio DB as the single source of truth
        from .portfolio_db import PortfolioDatabase
        db = PortfolioDatabase()
        portfolio = db.get_portfolio()
        trades_raw = db.get_trades()
        settings = self._load_json("bot_settings.json") or {}

        # Portfolio summary
        balance = portfolio.get("balance", 0)
        initial = portfolio.get("initial_balance", 10000)
        positions = portfolio.get("positions", {})
        stats = db.get_stats()
        total_value = stats.get("total_value", balance)
        open_count = len(positions)

        # Trades recientes (últimas 48h)
        cutoff = datetime.now(timezone.utc).timestamp() - (48 * 3600)
        recent_trades = []
        for t in trades_raw:
            ts_str = t.get("timestamp", "")
            try:
                ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts_dt.timestamp() > cutoff:
                    recent_trades.append(t)
            except Exception:
                continue

        # Ordenar por timestamp descendente
        recent_trades.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        # Analizar posiciones abiertas
        position_analysis = []
        for mid, pos in positions.items():
            entry = float(pos.get("entry_price", 0))
            current = float(pos.get("current_price", 0))
            pnl = float(pos.get("unrealized_pnl", 0))
            side = pos.get("side", "")
            end_date = pos.get("end_date", "")
            question = pos.get("question", "")[:60]

            # Determinar si es post-pivot (precio <0.15 o >0.85)
            is_post_pivot = entry < 0.15 or entry > 0.85
            is_extreme = current < 0.15 or current > 0.85

            # Riesgos
            risk_sl = "🔴" if pnl < -50 else ("🟡" if pnl < -20 else "🟢")
            risk_expiry = ""
            if end_date:
                try:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    days_left = (end_dt - datetime.now(timezone.utc)).days
                    risk_expiry = "🔴" if days_left <= 3 else ("🟡" if days_left <= 7 else "🟢")
                except Exception:
                    risk_expiry = "⚪"

            position_analysis.append({
                "market_id": mid,
                "question": question,
                "side": side,
                "entry_price": round(entry, 4),
                "current_price": round(current, 4),
                "unrealized_pnl": round(pnl, 2),
                "end_date": end_date,
                "is_post_pivot": is_post_pivot,
                "is_extreme": is_extreme,
                "risk_sl": risk_sl,
                "risk_expiry": risk_expiry,
            })

        # Validación post-pivot: verificar que no haya trades abiertos recientes en rango medio
        pivot_violations = []
        for t in recent_trades:
            if t.get("type") == "OPEN":
                price = float(t.get("price", 0))
                if 0.15 <= price <= 0.85:
                    pivot_violations.append({
                        "market_id": t.get("market_id"),
                        "price": price,
                        "timestamp": t.get("timestamp"),
                    })

        # Calculate drawdown and heat
        equity = db.get_equity_history()
        peak = max((e.get("value", 0) for e in equity), default=initial)
        drawdown = (peak - total_value) / peak if peak > 0 else 0.0
        heat = sum(p.get("cost_basis", 0) for p in positions.values()) / total_value if total_value > 0 else 0.0

        # Alertas
        alerts = []
        if total_value < 9000:
            alerts.append("🔴 PORTFOLIO < $9,000")
        elif total_value < 9500:
            alerts.append("🟡 PORTFOLIO < $9,500")
        if drawdown >= 0.20:
            alerts.append(f"🔴 DRAWDOWN CRÍTICO: {drawdown*100:.1f}%")
        elif drawdown >= 0.10:
            alerts.append(f"🟡 DRAWDOWN: {drawdown*100:.1f}%")
        if heat >= 0.15:
            alerts.append(f"🔴 HEAT ALTO: {heat*100:.1f}% del portfolio en riesgo")
        max_open = settings.get("max_open_positions", 5)
        if open_count >= max_open:
            alerts.append(f"🟡 Máximo de posiciones alcanzado ({open_count}/{max_open})")
        if pivot_violations:
            alerts.append(f"🔴 {len(pivot_violations)} posición(es) post-pivot en rango 15%-85%")
        if len(recent_trades) > 10 and sum(1 for t in recent_trades if t.get("type") == "CLOSE" and float(t.get("pnl", 0)) < 0) > 7:
            alerts.append("🟡 Win rate bajo en últimas 48h")
        # Alert: open positions with hard-stop risk (dominant side moved against)
        hard_stop_at_risk = sum(1 for p in position_analysis if p.get("risk_sl") == "🔴")
        if hard_stop_at_risk >= 2:
            alerts.append(f"🔴 {hard_stop_at_risk} posiciones en riesgo de SL duro")

        # Estado general
        if alerts and any("🔴" in a for a in alerts):
            status = "CRÍTICO"
            status_emoji = "🔴"
        elif alerts:
            status = "ATENCIÓN"
            status_emoji = "🟡"
        else:
            status = "NORMAL"
            status_emoji = "🟢"

        report = {
            "generated_at": now,
            "status": status,
            "status_emoji": status_emoji,
            "portfolio": {
                "balance": round(balance, 2),
                "initial_balance": initial,
                "total_value": round(total_value, 2),
                "total_return_pct": round(((total_value - initial) / initial) * 100, 2),
                "open_positions": open_count,
                "max_positions": max_open,
            },
            "stats": {
                "win_rate": stats.get("win_rate", 0),
                "wins": stats.get("wins", 0),
                "losses": stats.get("losses", 0),
                "total_pnl": round(stats.get("total_pnl", 0), 2),
            },
            "recent_trades": recent_trades[:10],
            "positions": position_analysis,
            "pivot_validation": {
                "violations_count": len(pivot_violations),
                "violations": pivot_violations,
            },
            "alerts": alerts,
            "settings_summary": {
                "position_size": settings.get("position_size_usdc", 200),
                "min_edge": settings.get("min_edge", 0.10),
                "max_expiry": settings.get("max_days_to_expiry", 30),
            },
            "risk_metrics": {
                "drawdown_pct": round(drawdown * 100, 2),
                "heat_pct": round(heat * 100, 2),
                "peak_value": round(peak, 2),
            },
        }

        # Guardar archivo
        filename = f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(self.reports_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        self.last_report_time = datetime.now(timezone.utc)
        return report

    def get_latest_report(self) -> Optional[Dict]:
        files = [f for f in os.listdir(self.reports_dir) if f.startswith("report_") and f.endswith(".json")]
        if not files:
            return None
        files.sort(reverse=True)
        latest = os.path.join(self.reports_dir, files[0])
        try:
            with open(latest, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def list_reports(self, limit: int = 20) -> List[str]:
        files = [f for f in os.listdir(self.reports_dir) if f.startswith("report_") and f.endswith(".json")]
        files.sort(reverse=True)
        return files[:limit]
