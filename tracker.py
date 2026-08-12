"""后台新成交探测：轮询每个跟踪的钱包，发现新成交就给它累加未读数（面板红圈）。

原先这里还负责 Telegram 推送，推送功能已移除，只保留面板上的红圈提醒。
因为不再依赖 Telegram，轮询改由网页服务（web.py 的 lifespan）驱动，
这样即使不开机器人（BOT_ENABLED=false）红圈也照常工作。

每轮只统计比游标更新的成交，首轮仅记录游标不计数，避免把历史记录一次性算成未读。
"""

from __future__ import annotations

import asyncio
import json
import logging

import chains
from chains.base import ActionsUnsupported, ChainError
from chains.hyperliquid import hyperliquid_state
from chains.portfolio import DEFAULT_EVM_CHAINS, evm_swaps

log = logging.getLogger("tracker")

EVM_CHAINS = {"eth", "bsc", "base", "arb", "polygon", "op"}


def _load_state(cursor: str | None) -> dict:
    """Parse the per-wallet cursor JSON; non-JSON (or empty) means first run."""
    try:
        d = json.loads(cursor) if cursor else {}
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


async def _wallet_events(wallet, config, http) -> list[dict]:
    """Merged HL fills + EVM swaps for one wallet, newest-first."""
    addr = wallet.address
    events: list[dict] = []

    try:  # Hyperliquid is keyless
        hl = await hyperliquid_state(http, addr, with_fills=True)
        events.extend(hl.get("fills", []) or [])
    except Exception as exc:  # noqa: BLE001 - one source failing shouldn't kill the scan
        log.debug("HL scan %s failed: %s", addr, exc)

    if config.moralis_api_key:
        try:
            sw = await evm_swaps(
                http, config.moralis_api_key, addr.lower(),
                list(DEFAULT_EVM_CHAINS), limit=20,
            )
            events.extend(sw.get("swaps", []) or [])
        except Exception as exc:  # noqa: BLE001
            log.debug("EVM swaps scan %s failed: %s", addr, exc)

    events.sort(key=lambda e: e.get("timestamp") or 0, reverse=True)
    return events


async def _scan_legacy(wallet, config, http, db) -> None:
    """Non-EVM chains (sol/btc): count new actions via the chain client,
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
    if not wallet.cursor:  # 首轮只记游标，不计数
        db.set_cursor(wallet.id, newest)
        return
    fresh = 0
    for a in actions:
        if a.tx_hash == wallet.cursor:
            break
        fresh += 1
    if not fresh:
        return
    db.set_cursor(wallet.id, newest)
    db.bump_unread(wallet.id, fresh)  # 面板红圈未读数


async def _scan_wallet(wallet, config, http, db) -> None:
    if wallet.chain not in EVM_CHAINS:
        return await _scan_legacy(wallet, config, http, db)
    events = await _wallet_events(wallet, config, http)
    if not events:
        return

    state = _load_state(wallet.cursor)
    newest_ts = events[0].get("timestamp") or 0

    # 首轮（游标里还没有 ts）：只记录最新时间，不计数，避免把整段历史算成未读。
    if "ts" not in state:
        db.set_cursor(wallet.id, json.dumps({"ts": newest_ts}))
        return

    last_ts = int(state.get("ts") or 0)
    fresh = [e for e in events if (e.get("timestamp") or 0) > last_ts]
    if not fresh:
        return
    db.set_cursor(wallet.id, json.dumps({"ts": max(newest_ts, last_ts)}))

    # 小额成交不计入红圈，避免刷屏
    n = sum(1 for e in fresh if (e.get("value_usd") or 0) >= config.alert_min_usd)
    if n:
        db.bump_unread(wallet.id, n)


async def scan_once(config, http, db) -> None:
    """Walk every watched wallet once, bumping unread counts for new trades."""
    for wallet in db.list_wallets():
        try:
            await _scan_wallet(wallet, config, http, db)
        except Exception as exc:  # noqa: BLE001
            log.warning("unexpected scan error %s: %s", wallet.address, exc)
        await asyncio.sleep(0.5)  # gentle pacing across wallets / APIs


async def run_poller(config, http, db) -> None:
    """Background loop: scan for new trades every ``POLL_INTERVAL`` seconds."""
    await asyncio.sleep(15)  # 让网页先起来，避免启动瞬间抢免费接口额度
    while True:
        try:
            await scan_once(config, http, db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let one round kill the loop
            log.warning("scan round failed: %s", exc)
        await asyncio.sleep(config.poll_interval)
