"""Entrypoint: runs the Telegram bot (with the auto-tracking poller) and the
FastAPI dashboard together.

The bot runs in the main thread via PTB's ``run_polling`` (it owns the main
event loop and signal handling). The web dashboard runs in a daemon thread with
its own event loop and its own httpx client. Both share the thread-safe
``WalletDB``.
"""

from __future__ import annotations

import logging
import threading

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler

import bot as handlers
from config import Config, load_config
from db import WalletDB
from tracker import poll_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


def build_application(config: Config, db: WalletDB) -> Application:
    async def post_init(app: Application) -> None:
        app.bot_data["config"] = config
        app.bot_data["db"] = db
        app.bot_data["http"] = httpx.AsyncClient()
        log.info("bot initialized; tracking %d wallets", len(db.list_wallets()))

    async def post_shutdown(app: Application) -> None:
        http = app.bot_data.get("http")
        if http is not None:
            await http.aclose()

    app = (
        Application.builder()
        .token(config.tg_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("chains", handlers.cmd_chains))
    app.add_handler(CommandHandler("add", handlers.cmd_add))
    app.add_handler(CommandHandler("remove", handlers.cmd_remove))
    app.add_handler(CommandHandler("list", handlers.cmd_list))
    app.add_handler(CommandHandler("balance", handlers.cmd_balance))
    app.add_handler(CommandHandler("history", handlers.cmd_history))
    app.add_error_handler(handlers.on_error)

    # Auto-tracking poller.
    app.job_queue.run_repeating(
        poll_job, interval=config.poll_interval, first=15, name="poll"
    )
    return app


def start_web_thread(config: Config, db: WalletDB) -> None:
    """Launch the FastAPI dashboard in a daemon thread (own event loop)."""
    import uvicorn

    from web import create_web_app

    def run() -> None:
        web_app = create_web_app(config, db)
        server = uvicorn.Server(
            uvicorn.Config(
                web_app,
                host=config.web_host,
                port=config.web_port,
                log_level="info",
                # The bot owns process signals; the web server must not grab them.
                lifespan="on",
            )
        )
        server.install_signal_handlers = lambda: None
        server.run()

    thread = threading.Thread(target=run, name="web", daemon=True)
    thread.start()
    log.info("dashboard at http://%s:%d", config.web_host, config.web_port)


def main() -> None:
    config = load_config()
    db = WalletDB(config.db_path)

    if config.web_enabled:
        start_web_thread(config, db)

    app = build_application(config, db)
    log.info("starting Telegram bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()
