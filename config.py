"""Application configuration loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv optional; env vars still work
    pass


@dataclass
class Config:
    tg_token: str
    tg_proxy: str                # proxy URL for Telegram (needed where TG is blocked)
    alert_chat_id: str           # default chat stored on wallets added from the panel
    allowed_chat_ids: set[str]   # chats allowed to control the bot (empty = any)
    etherscan_api_key: str
    helius_api_key: str
    moralis_api_key: str
    poll_interval: int           # seconds between new-trade scans
    alert_min_usd: float         # ignore trades below this USD value (anti-spam)
    db_path: str
    bot_enabled: bool            # run the Telegram bot commands (set false for web-only)
    web_enabled: bool            # serve the FastAPI dashboard alongside the bot
    web_host: str
    web_port: int
    web_token: str               # optional access token for the dashboard ("" = open)


def _clean(value: str) -> str:
    return (value or "").strip()


def load_config() -> Config:
    _falsey = ("0", "false", "no", "off")
    bot_enabled = (_clean(os.getenv("BOT_ENABLED")) or "true").lower() not in _falsey

    token = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
    # 机器人只提供查询命令（已无推送），关掉它就不需要 token —— 纯网页面板可直接跑。
    if not token and bot_enabled:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN 未设置。请复制 .env.example 为 .env 并填写，"
            "或设 BOT_ENABLED=false 只运行网页面板。"
        )
    chat = _clean(os.getenv("TELEGRAM_CHAT_ID"))
    allowed_raw = _clean(os.getenv("ALLOWED_CHAT_IDS")) or chat
    allowed = {c.strip() for c in allowed_raw.split(",") if c.strip()}

    try:
        poll = int(_clean(os.getenv("POLL_INTERVAL")) or "60")
    except ValueError:
        poll = 60
    poll = max(poll, 20)  # be polite to free APIs

    def _float(name: str, default: float) -> float:
        try:
            return float(_clean(os.getenv(name)) or default)
        except ValueError:
            return default

    alert_min = _float("ALERT_MIN_USD", 10000)
    tg_proxy = _clean(os.getenv("TELEGRAM_PROXY")) or _clean(os.getenv("BOT_PROXY"))

    try:
        web_port = int(_clean(os.getenv("WEB_PORT")) or "8000")
    except ValueError:
        web_port = 8000
    web_enabled = (_clean(os.getenv("WEB_ENABLED")) or "true").lower() not in _falsey

    return Config(
        tg_token=token,
        tg_proxy=tg_proxy,
        alert_chat_id=chat,
        allowed_chat_ids=allowed,
        etherscan_api_key=_clean(os.getenv("ETHERSCAN_API_KEY")),
        helius_api_key=_clean(os.getenv("HELIUS_API_KEY")),
        moralis_api_key=_clean(os.getenv("MORALIS_API_KEY")),
        poll_interval=poll,
        alert_min_usd=alert_min,
        db_path=_clean(os.getenv("DB_PATH")) or "wallets.db",
        bot_enabled=bot_enabled,
        web_enabled=web_enabled,
        web_host=_clean(os.getenv("WEB_HOST")) or "127.0.0.1",
        web_port=web_port,
        web_token=_clean(os.getenv("WEB_TOKEN")),
    )
