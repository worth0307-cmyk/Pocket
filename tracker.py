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

import chains
from chains.base import ActionsUnsupported, ChainError
from chains.hyperliquid import hyperliquid_state
from chains.portfolio import DEFAULT_EVM_CHAINS, evm_swaps
from formatting import format_alert, format_trade_alert

log = logging.getLogger("tracker")

EVM_CHAINS = {"eth", "bsc", "base", "arb", "polygon", "op"}

# 每地址每币最后见到的杠杆。仓位平掉后持仓里就没杠杆了，
# 靠这个记忆让「平仓」推送也能带上杠杆（重启后清空，可接受）。
_lev_memory: dict[str, dict] = {}


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
            # 该币当前持仓的杠杆（HL 成交不含历史杠杆，推送里标当前设置）。
            # 平仓后持仓里已无该币，用上次轮询记住的杠杆兜底。
            remembered = _lev_memory.setdefault(addr.lower(), {})
            for p in hl.get("positions") or []:
                if p.get("leverage"):
                    remembered[p.get("coin")] = p.get("leverage")
            for f in hl.get("fills", []) or []:
                events.append({
                    **f, "venue": f.get("venue") or "HL",
                    "leverage": remembered.get(f.get("token_symbol")),
                })
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


async def _poll_legacy(wallet, config, http, bot, db) -> None:
    """Non-EVM chains (sol/btc): alert on new actions via the chain client,
    using the original tx-hash cursor (these chains have no HL/DEX trades)."""
    client = chains.get_client(wallet.chain, config, http)
    if client is None:
        return
    try:
        actions = await client.get_actions(wallet.address, limit=20)
    except (ActionsUnsupported, ChainError):
        return
    if not actions:
        return
    newest = actions[0].tx_hash
    if not wallet.cursor:
        db.set_cursor(wallet.id, newest)
        return
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
    sent = 0
    for action in reversed(fresh[: config.max_alerts_per_poll]):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=format_alert(action, wallet),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("send alert failed: %s", exc)
        await asyncio.sleep(0.3)
    if sent:
        db.bump_unread(wallet.id, sent)  # 面板红圈未读数


async def _poll_wallet(wallet, config, http, bot, db) -> None:
    if wallet.chain not in EVM_CHAINS:
        return await _poll_legacy(wallet, config, http, bot, db)
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
    sent = 0
    for e in sendable[: config.max_alerts_per_poll]:
        is_large = (e.get("value_usd") or 0) >= config.alert_large_usd
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=format_trade_alert(e, wallet, tier, is_large),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001 - never let one alert kill the poll
            log.warning("send alert failed: %s", exc)
        await asyncio.sleep(0.3)
    if sent:
        db.bump_unread(wallet.id, sent)  # 面板红圈未读数


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
