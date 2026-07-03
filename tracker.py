"""Auto-tracking poller: push Telegram alerts for new trades on watched wallets.

Each tick walks every watched wallet, pulls its recent Hyperliquid fills (and
EVM DEX swaps where Moralis is configured), and alerts on anything newer than
the stored cursor. A single unified trade alert covers buy/sell, open/close
(from the fill's ``dir``), large actions (≥ threshold, marked 🔥) and realized
PnL on closes (``closed_pnl``). The wallet's tier (巨鲸/超大户/…) is derived from
its total assets, matching the dashboard badge.
"""

from __future__ import annotations

import asyncio
import json
import logging

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from chains.hyperliquid import hyperliquid_state
from chains.portfolio import DEFAULT_EVM_CHAINS, evm_swaps
from formatting import format_trade_alert

log = logging.getLogger("tracker")

EVM_CHAINS = {"eth", "bsc", "base", "arb", "polygon", "op"}


def _tier(usd: float) -> tuple[str, str]:
    usd = usd or 0
    if usd >= 1e8:
        return ("🐳", "巨鲸")
    if usd >= 1e7:
        return ("🐋", "超大户")
    if usd >= 1e6:
        return ("🦈", "大户")
    if usd >= 1e5:
        return ("🐬", "中户")
    if usd >= 1e4:
        return ("🐟", "小户")
    return ("🦐", "散户")


def _load_state(cursor: str | None) -> dict:
    """Parse the per-wallet cursor JSON; non-JSON (or empty) means first run."""
    try:
        d = json.loads(cursor) if cursor else {}
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


async def _wallet_events(wallet, config, http) -> tuple[list[dict], float]:
    """Merged HL fills + EVM swaps (newest-first) and the wallet total for its tier."""
    addr = wallet.address
    events: list[dict] = []
    total = 0.0

    if wallet.chain in EVM_CHAINS:
        try:  # Hyperliquid is keyless
            hl = await hyperliquid_state(http, addr, with_fills=True)
            total += hl.get("total_usd", 0) or 0
            for f in hl.get("fills", []) or []:
                events.append({**f, "venue": f.get("venue") or "HL"})
        except Exception as exc:  # noqa: BLE001 - one source failing shouldn't kill the poll
            log.debug("HL poll %s failed: %s", addr, exc)

        if config.moralis_api_key:
            try:
                sw = await evm_swaps(
                    http, config.moralis_api_key, addr.lower(),
                    list(DEFAULT_EVM_CHAINS), limit=20,
                )
                for s in sw.get("swaps", []) or []:
                    events.append({**s, "venue": s.get("chain")})
            except Exception as exc:  # noqa: BLE001
                log.debug("EVM swaps poll %s failed: %s", addr, exc)

    events.sort(key=lambda e: e.get("timestamp") or 0, reverse=True)
    return events, total


async def _poll_wallet(wallet, config, http, bot, db) -> None:
    events, total = await _wallet_events(wallet, config, http)
    if not events:
        return

    state = _load_state(wallet.cursor)
    newest_ts = events[0].get("timestamp") or 0

    # First run (no valid cursor state): record the newest timestamp without
    # alerting, so we don't replay the wallet's whole history.
    if "ts" not in state:
        db.set_cursor(wallet.id, json.dumps({"ts": newest_ts}))
        return

    last_ts = int(state.get("ts") or 0)
    fresh = [e for e in events if (e.get("timestamp") or 0) > last_ts]
    if not fresh:
        return
    db.set_cursor(wallet.id, json.dumps({"ts": max(newest_ts, last_ts)}))

    chat_id = wallet.chat_id or config.alert_chat_id
    if not chat_id:
        return

    tier = _tier(total)
    # Oldest-first so alerts arrive chronologically; drop sub-threshold noise; cap.
    sendable = [
        e for e in reversed(fresh)
        if (e.get("value_usd") or 0) >= config.alert_min_usd
    ]
    for e in sendable[: config.max_alerts_per_poll]:
        is_large = (e.get("value_usd") or 0) >= config.alert_large_usd
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=format_trade_alert(e, wallet, tier, is_large),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001 - never let one alert kill the poll
            log.warning("send alert failed: %s", exc)
        await asyncio.sleep(0.3)


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.application.bot_data
    config = data["config"]
    http = data["http"]
    db = data["db"]
    bot = context.bot

    for wallet in db.list_wallets():
        try:
            await _poll_wallet(wallet, config, http, bot, db)
        except Exception as exc:  # noqa: BLE001
            log.warning("unexpected poll error %s: %s", wallet.address, exc)
        await asyncio.sleep(0.5)  # gentle pacing across wallets / APIs
