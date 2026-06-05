"""
MiroFish Backend - Flask应用工厂
"""

import os
import warnings

# 抑制 multiprocessing resource_tracker 的警告（来自第三方库如 transformers）
# 需要在所有其他导入之前设置
warnings.filterwarnings("ignore", message=".*resource_tracker.*")

from flask import Flask, request
from flask_cors import CORS

from .config import Config
from .utils.logger import setup_logger, get_logger


def create_app(config_class=Config):
    """Flask应用工厂函数"""
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    # 设置JSON编码：确保中文直接显示（而不是 \uXXXX 格式）
    # Flask >= 2.3 使用 app.json.ensure_ascii，旧版本使用 JSON_AS_ASCII 配置
    if hasattr(app, 'json') and hasattr(app.json, 'ensure_ascii'):
        app.json.ensure_ascii = False
    
    # 设置日志
    logger = setup_logger('mirofish')
    
    # 只在 reloader 子进程中打印启动信息（避免 debug 模式下打印两次）
    is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    debug_mode = app.config.get('DEBUG', False)
    should_log_startup = not debug_mode or is_reloader_process
    
    if should_log_startup:
        logger.info("=" * 50)
        logger.info("MiroFish Backend 启动中...")
        logger.info("=" * 50)
    
    # 启用CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # 注册模拟进程清理函数（仅在camel-oasis可用时）
    try:
        from .services.simulation_runner import SimulationRunner
        SimulationRunner.register_cleanup()
        if should_log_startup:
            logger.info("已注册模拟进程清理函数")
    except ImportError:
        if should_log_startup:
            logger.info("camel-oasis no disponible — modo bot ligero activo")
    
    # 请求日志中间件
    @app.before_request
    def log_request():
        logger = get_logger('mirofish.request')
        logger.debug(f"请求: {request.method} {request.path}")
        if request.content_type and 'json' in request.content_type:
            logger.debug(f"请求体: {request.get_json(silent=True)}")
    
    @app.after_request
    def log_response(response):
        logger = get_logger('mirofish.request')
        logger.debug(f"响应: {response.status_code}")
        return response
    
    # 注册蓝图
    from .api import graph_bp, simulation_bp, report_bp, polymarket_bp
    app.register_blueprint(graph_bp, url_prefix='/api/graph')
    app.register_blueprint(simulation_bp, url_prefix='/api/simulation')
    app.register_blueprint(report_bp, url_prefix='/api/report')
    app.register_blueprint(polymarket_bp, url_prefix='/api/polymarket')
    
    # 健康检查
    @app.route('/health')
    def health():
        try:
            from .services.polymarket.autonomous_pipeline import get_bot_status
            bot_status = get_bot_status()
        except Exception:
            bot_status = {"running": False}
        return {
            'status': 'ok',
            'service': 'MiroFish Backend',
            'bot': {
                'running': bot_status.get('running', False),
            }
        }

    @app.route('/')
    def index():
        return {'service': 'MiroFish Backend', 'status': 'ok', 'docs': '/health'}
    
    # ── Auto-start the autonomous bot ─────────────────────────────────────
    try:
        from .services.polymarket.autonomous_pipeline import start_bot
        start_bot()
        if should_log_startup:
            logger.info("Autonomous bot auto-started")
    except Exception as e:
        if should_log_startup:
            logger.warning(f"Bot auto-start failed: {e}")

    # ── Watchdog: restart bot if thread dies (Railway won't restart container
    #    if Flask health-check is still OK, but the bot thread may crash) ──
    def _bot_watchdog():
        import threading
        import time
        watchdog_logger = get_logger('mirofish.watchdog')
        while True:
            time.sleep(300)  # 5 minutes
            try:
                from .services.polymarket.autonomous_pipeline import (
                    get_bot_status, start_bot
                )
                status = get_bot_status()
                if not status.get("running"):
                    watchdog_logger.warning(
                        "Watchdog: bot thread not running — restarting"
                    )
                    start_bot()
            except Exception as e:
                watchdog_logger.error(f"Watchdog error: {e}")

    _watchdog = threading.Thread(target=_bot_watchdog, daemon=True, name="bot-watchdog")
    _watchdog.start()

    if should_log_startup:
        logger.info("MiroFish Backend 启动完成")

    return app

