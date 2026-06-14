"""
Paper trading API endpoints for Polymarket integration.
"""

import os
import traceback
from flask import request, jsonify

from . import polymarket_bp
from ..services.polymarket import PolymarketPipeline, MarketFetcher, PaperTrader, PortfolioDatabase
from ..services.polymarket.autonomous_pipeline import (
    run_once, start_bot, stop_bot, get_bot_status, get_runs,
    load_settings, save_settings,
)
from ..services.polymarket.bot_reporter import BotReporter
from ..services.polymarket.backtester import Backtester
from ..services.report_agent import ReportManager
from ..utils.logger import get_logger
logger = get_logger("mirofish.api.polymarket")


# ---------- Market data ----------

@polymarket_bp.route("/markets", methods=["GET"])
def get_markets():
    """Returns a list of active Polymarket markets (no auth required)."""
    try:
        limit = request.args.get("limit", 20, type=int)
        min_volume = request.args.get("min_volume", 1000.0, type=float)
        min_liquidity = request.args.get("min_liquidity", 500.0, type=float)
        category = request.args.get("category")

        fetcher = MarketFetcher()
        markets = fetcher.get_active_markets(
            limit=limit,
            min_volume=min_volume,
            min_liquidity=min_liquidity,
            category=category,
        )
        return jsonify({"success": True, "data": [m.to_dict() for m in markets], "count": len(markets)})
    except Exception as e:
        logger.error(f"get_markets error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- Portfolio ----------

@polymarket_bp.route("/portfolio", methods=["GET"])
def get_portfolio():
    """Returns current paper trading portfolio, stats, and equity history."""
    try:
        db = PortfolioDatabase()
        portfolio = db.get_portfolio()
        stats = db.get_stats()
        equity = db.get_equity_history()

        return jsonify({
            "success": True,
            "data": {
                "portfolio": portfolio,
                "stats": stats,
                "equity_history": equity[-200:],  # last 200 points for chart
            },
        })
    except Exception as e:
        logger.error(f"get_portfolio error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/portfolio/reset", methods=["POST"])
def reset_portfolio():
    """Resets the paper trading portfolio to initial state."""
    try:
        db = PortfolioDatabase()
        db.reset_portfolio()
        return jsonify({"success": True, "message": "Portfolio reset to 10,000 USDC"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- Trades ----------

@polymarket_bp.route("/trades", methods=["GET"])
def get_trades():
    """Returns all trade history."""
    try:
        db = PortfolioDatabase()
        trades = db.get_trades()
        trades_sorted = sorted(trades, key=lambda t: t.get("timestamp", ""), reverse=True)
        return jsonify({"success": True, "data": trades_sorted, "count": len(trades_sorted)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/trades/close", methods=["POST"])
def close_trade():
    """Manually closes an open position at a given price."""
    try:
        data = request.get_json() or {}
        market_id = data.get("market_id")
        exit_price = data.get("exit_price")
        reason = data.get("reason", "manual")

        if not market_id or exit_price is None:
            return jsonify({"success": False, "error": "market_id and exit_price required"}), 400

        trader = PaperTrader()

        # try to get live price if not provided
        if exit_price == "live":
            fetcher = MarketFetcher()
            db = PortfolioDatabase()
            portfolio = db.get_portfolio()
            pos = portfolio.get("positions", {}).get(market_id)
            if pos:
                markets = fetcher.get_active_markets(limit=100)
                market = next((m for m in markets if m.id == market_id), None)
                if market:
                    token_id = market.yes_token_id if pos["side"] == "YES" else market.no_token_id
                    live = fetcher.get_market_price(token_id) if token_id else None
                    exit_price = live if live is not None else pos["current_price"]
                else:
                    exit_price = pos.get("current_price", pos["entry_price"])

        trade = trader.close_position(
            market_id=market_id,
            exit_price=float(exit_price),
            reason=reason,
        )
        return jsonify({"success": True, "data": trade})
    except Exception as e:
        logger.error(f"close_trade error: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- Pipeline ----------

@polymarket_bp.route("/pipeline/run", methods=["POST"])
def run_pipeline():
    """
    Runs the full MiroFish → Polymarket paper trading pipeline.

    Request body:
        {
            "report_id": "report_xxxx",       // optional: use existing report
            "report_markdown": "...",          // optional: provide report directly
            "simulation_id": "sim_xxxx",       // optional
            "max_markets": 20,                 // optional
            "position_size": 200,              // optional, USDC per trade
            "min_edge": 0.06,                  // optional
            "dry_run": false                   // optional: simulate without trading
        }
    """
    try:
        data = request.get_json() or {}

        report_markdown = data.get("report_markdown")
        report_id = data.get("report_id")
        simulation_id = data.get("simulation_id")
        max_markets = data.get("max_markets", 20)
        position_size = data.get("position_size", 200.0)
        min_edge = data.get("min_edge", 0.06)
        dry_run = data.get("dry_run", False)

        # load report from DB if not provided directly
        if not report_markdown and report_id:
            report = ReportManager.get_report(report_id)
            if not report:
                return jsonify({"success": False, "error": f"Report {report_id} not found"}), 404
            report_markdown = report.markdown_content
            if not simulation_id:
                simulation_id = report.simulation_id

        if not report_markdown:
            return jsonify({
                "success": False,
                "error": "Provide either report_id or report_markdown",
            }), 400

        pipeline = PolymarketPipeline(
            position_size=float(position_size),
            min_edge=float(min_edge),
        )

        result = pipeline.run_from_report(
            report_markdown=report_markdown,
            simulation_id=simulation_id,
            report_id=report_id,
            max_markets=int(max_markets),
            dry_run=bool(dry_run),
        )

        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"run_pipeline error: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@polymarket_bp.route("/pipeline/refresh", methods=["POST"])
def refresh_positions():
    """Fetches latest prices and updates unrealized P&L for all open positions."""
    try:
        pipeline = PolymarketPipeline()
        result = pipeline.refresh_positions()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── Autonomous Bot ─────────────────────────────────────────────────────────────

@polymarket_bp.route("/bot/status", methods=["GET"])
def bot_status():
    """Returns bot running state, settings, and last run summary."""
    try:
        return jsonify({"success": True, "data": get_bot_status()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/bot/run-once", methods=["POST"])
def bot_run_once():
    """Runs one full bot cycle synchronously and returns the run log."""
    try:
        result = run_once()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"bot_run_once error: {e}\n{traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@polymarket_bp.route("/bot/start", methods=["POST"])
def bot_start():
    """Starts the background bot (runs cycles automatically on interval)."""
    try:
        result = start_bot()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/bot/stop", methods=["POST"])
def bot_stop():
    """Stops the background bot."""
    try:
        result = stop_bot()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/bot/runs", methods=["GET"])
def bot_runs():
    """Returns the last N bot run summaries with logs."""
    try:
        limit = request.args.get("limit", 20, type=int)
        return jsonify({"success": True, "data": get_runs(limit)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/bot/settings", methods=["GET"])
def get_bot_settings():
    """Returns current bot settings."""
    try:
        return jsonify({"success": True, "data": load_settings()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/bot/settings", methods=["POST"])
def update_bot_settings():
    """Updates bot settings (partial update supported)."""
    try:
        updates = request.get_json() or {}
        current = load_settings()
        current.update(updates)
        save_settings(current)
        return jsonify({"success": True, "data": current})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── Reports ────────────────────────────────────────────────────────────────────

# Reporter instance using the same data dir as portfolio_db
from ..services.polymarket.portfolio_db import DATA_DIR as _PT_DATA_DIR

_reporter = BotReporter(_PT_DATA_DIR)


@polymarket_bp.route("/report", methods=["GET"])
def get_latest_report():
    """Devuelve el último reporte generado automáticamente."""
    try:
        report = _reporter.get_latest_report()
        if not report:
            return jsonify({"success": False, "error": "No reports generated yet"}), 404
        return jsonify({"success": True, "data": report})
    except Exception as e:
        logger.error(f"get_latest_report error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/reports", methods=["GET"])
def list_reports():
    """Lista los últimos N reportes generados."""
    try:
        limit = request.args.get("limit", 20, type=int)
        files = _reporter.list_reports(limit=limit)
        return jsonify({
            "success": True,
            "data": {
                "reports": files,
                "count": len(files),
                "latest": files[0] if files else None,
            }
        })
    except Exception as e:
        logger.error(f"list_reports error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/reports/<filename>", methods=["GET"])
def get_specific_report(filename):
    """Devuelve un reporte específico por nombre de archivo."""
    try:
        # Seguridad: solo permitir filenames que empiecen con report_ y terminen en .json
        if not filename.startswith("report_") or not filename.endswith(".json"):
            return jsonify({"success": False, "error": "Invalid filename"}), 400

        filepath = os.path.join(_reporter.reports_dir, filename)
        if not os.path.exists(filepath):
            return jsonify({"success": False, "error": "Report not found"}), 404

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify({"success": True, "data": data})
    except Exception as e:
        logger.error(f"get_specific_report error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@polymarket_bp.route("/report/force", methods=["POST"])
def force_report():
    """Fuerza la generación de un reporte manualmente."""
    try:
        report = _reporter.generate_report()
        return jsonify({"success": True, "data": report})
    except Exception as e:
        logger.error(f"force_report error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- Backtest ----------

@polymarket_bp.route("/backtest/run", methods=["POST"])
def run_backtest():
    """
    Runs a historical backtest of the contrarian strategy.
    Body/Query params:
      - limit: number of top markets to test (default 10)
      - fidelity: CLOB history candle size in minutes (default 720 = 12h)
      - min_volume / min_liquidity: market filters
      - mode: "portfolio" (capital-aware) or "signal" (unconstrained, default "portfolio")
      - initial_balance: for portfolio mode (default 10000)
    """
    try:
        data = request.get_json(silent=True) or {}
        limit = data.get("limit", request.args.get("limit", 10, type=int))
        fidelity = data.get("fidelity", request.args.get("fidelity", 720, type=int))
        min_volume = data.get("min_volume", request.args.get("min_volume", 5000.0, type=float))
        min_liquidity = data.get("min_liquidity", request.args.get("min_liquidity", 1000.0, type=float))
        mode = data.get("mode", request.args.get("mode", "portfolio"))
        initial_balance = data.get("initial_balance", request.args.get("initial_balance", 10_000.0, type=float))

        settings = load_settings()
        backtester = Backtester(settings=settings, fidelity_minutes=fidelity)

        if mode == "portfolio":
            results = backtester.run_portfolio(
                limit=limit,
                min_volume=min_volume,
                min_liquidity=min_liquidity,
                initial_balance=initial_balance,
            )
        else:
            results = backtester.run(
                limit=limit,
                min_volume=min_volume,
                min_liquidity=min_liquidity,
            )

        return jsonify({"success": True, "data": results})
    except Exception as e:
        logger.error(f"run_backtest error: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
