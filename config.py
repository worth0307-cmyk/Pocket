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
    alert_chat_id: str           # default chat to send auto-tracking alerts to
    allowed_chat_ids: set[str]   # chats allowed to control the bot (empty = any)
    etherscan_api_key: str
    helius_api_key: str
    poll_interval: int           # seconds between auto-tracking polls
    db_path: str
    max_alerts_per_poll: int     # cap alerts per wallet per poll to avoid floods
    web_enabled: bool            # serve the FastAPI dashboard alongside the bot
    web_host: str
    web_port: int
    web_token: str               # optional access token for the dashboard ("" = open)


def _clean(value: str) -> str:
    return (value or "").strip()


def load_config() -> Config:
    token = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN 未设置。请复制 .env.example 为 .env 并填写。"
        )
    chat = _clean(os.getenv("TELEGRAM_CHAT_ID"))
    allowed_raw = _clean(os.getenv("ALLOWED_CHAT_IDS")) or chat
    allowed = {c.strip() for c in allowed_raw.split(",") if c.strip()}

    try:
        poll = int(_clean(os.getenv("POLL_INTERVAL")) or "60")
    except ValueError:
        poll = 60
    poll = max(poll, 20)  # be polite to free APIs

    try:
        web_port = int(_clean(os.getenv("WEB_PORT")) or "8000")
    except ValueError:
        web_port = 8000
    web_enabled = (_clean(os.getenv("WEB_ENABLED")) or "true").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    return Config(
        tg_token=token,
        alert_chat_id=chat,
        allowed_chat_ids=allowed,
        etherscan_api_key=_clean(os.getenv("ETHERSCAN_API_KEY")),
        helius_api_key=_clean(os.getenv("HELIUS_API_KEY")),
        poll_interval=poll,
        db_path=_clean(os.getenv("DB_PATH")) or "wallets.db",
        max_alerts_per_poll=10,
        web_enabled=web_enabled,
        web_host=_clean(os.getenv("WEB_HOST")) or "127.0.0.1",
        web_port=web_port,
        web_token=_clean(os.getenv("WEB_TOKEN")),
    )
