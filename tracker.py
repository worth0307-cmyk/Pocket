"""Auto-tracking poller: detect new actions on watched wallets and alert.

Designed to be driven by python-telegram-bot's JobQueue (run_repeating). On
each tick it walks every watched wallet, fetches recent actions, and pushes a
Telegram alert for anything newer than the stored cursor.
"""

from __future__ import annotations

import asyncio
import logging

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import chains
from chains.base import ActionsUnsupported, ChainError
from formatting import format_alert

log = logging.getLogger("tracker")


async def _poll_wallet(wallet, config, http, bot, db) -> None:
    client = chains.get_client(wallet.chain, config, http)
    if client is None:
        return
    try:
        actions = await client.get_actions(wallet.address, limit=20)
    except ActionsUnsupported:
        return  # e.g. Solana without Helius key — silently skip auto-tracking
    except ChainError as exc:
        log.warning("poll %s/%s failed: %s", wallet.chain, wallet.address, exc)
        return

    if not actions:
        return

    newest = actions[0].tx_hash

    # First time we see this wallet: record the cursor without alerting so we
    # don't replay its entire history.
    if not wallet.cursor:
        db.set_cursor(wallet.id, newest)
        return

    # Collect everything newer than the stored cursor (newest-first list).
    fresh = []
    for a in actions:
        if a.tx_hash == wallet.cursor:
            break
        fresh.append(a)

    if not fresh:
        return

    db.set_cursor(wallet.id, newest)

    chat_id = wallet.chat_id or config.alert_chat_id
    if not chat_id:
        return

    # Oldest-first so alerts arrive in chronological order; cap to avoid floods.
    for action in reversed(fresh[: config.max_alerts_per_poll]):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=format_alert(action, wallet),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001 - never let one alert kill the poll
            log.warning("send alert failed: %s", exc)
        await asyncio.sleep(0.3)


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    data = context.application.bot_data
    config = data["config"]
    http = data["http"]
    db = data["db"]

    wallets = db.list_wallets()
    for wallet in wallets:
        try:
            await _poll_wallet(wallet, config, http, bot, db)
        except Exception as exc:  # noqa: BLE001
            log.warning("unexpected poll error %s: %s", wallet.address, exc)
        await asyncio.sleep(0.5)  # gentle pacing across wallets / APIs
