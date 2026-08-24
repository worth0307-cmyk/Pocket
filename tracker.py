"""后台新成交探测：轮询每个跟踪的钱包，发现新成交就累加未读数（面板红圈），
并按需推送 Telegram。

轮询由网页服务（web.py 的 lifespan）驱动，红圈对所有钱包生效。
Telegram 推送是**定向**的：只推名称以 PUSH_PREFIX（默认 ZR）开头的钱包，
其余钱包只亮红圈不打扰。推送直接走 Bot HTTP API，不依赖 python-telegram-bot 的
长轮询，所以 BOT_ENABLED=false（不开机器人命令）时推送照样能发。

同一钱包在短时间内连续的相同动作（同币种 + 同方向，间隔 ≤ PUSH_MERGE_WINDOW）
会合并成一条推送：HL 常把一笔大单拆成几十个成交，不合并会刷屏。

**只查 Hyperliquid**（免费公开接口、无 key、不限额）。这里曾经还顺带拉 Moralis 的
链上 swaps，但那是按「钱包 × 6 条链」全量轮询的：6 个钱包每分钟就是 36 次请求、
每天 5 万次，免费档 40K CU/月 不到一天就烧光（实测把额度打到 103%）。
Moralis 改为只在用户点开钱包看详情时按需调用，那边有缓存、量很小。
代价：红圈不统计纯链上 DEX 兑换，只统计 HL 成交。

每轮只统计比游标更新的成交，首轮仅记录游标不计数，避免把历史记录一次性算成未读。
"""

from __future__ import annotations

import asyncio
import json
import logging

import chains
from chains.base import ActionsUnsupported, ChainError
from chains.hyperliquid import hyperliquid_state
from formatting import format_trade_alert

log = logging.getLogger("tracker")

EVM_CHAINS = {"eth", "bsc", "base", "arb", "polygon", "op"}

# 每地址每币最后见到的杠杆。仓位平掉后持仓里就没杠杆了，
# 靠这个记忆让「平仓」推送也能带上杠杆（重启后清空，可接受）。
_lev_memory: dict[str, dict] = {}


def _tier(usd: float) -> tuple[str, str]:
    """钱包等级，与面板 walletTier() 同一套阈值。"""
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


def wants_push(wallet, config) -> bool:
    """该钱包是否要推 Telegram：名称以 PUSH_PREFIX 开头（留空=全推）。"""
    prefix = (config.push_prefix or "").strip()
    if not prefix:
        return True
    return (wallet.label or "").strip().upper().startswith(prefix.upper())


def merge_trades(fresh: list[dict], window: int) -> list[dict]:
    """把短时间内连续的相同动作合并成一条。

    分组键是 币种 + 方向（开多/平空/买入…）；组内按时间排序，相邻两笔间隔超过
    ``window`` 秒就断开成新的一簇。合并后数量与金额取合计，价格取名义金额加权均价，
    时间取该簇最后一笔，``count`` 记录合并了几笔。
    """
    if not fresh:
        return []
    ordered = sorted(
        fresh, key=lambda e: (
            str(e.get("token_symbol") or ""), str(e.get("dir") or e.get("type") or ""),
            e.get("timestamp") or 0,
        )
    )
    clusters: list[list[dict]] = []
    for e in ordered:
        key = (e.get("token_symbol"), e.get("dir") or e.get("type"))
        ts = e.get("timestamp") or 0
        if clusters:
            last = clusters[-1][-1]
            same = (last.get("token_symbol"), last.get("dir") or last.get("type")) == key
            close = window > 0 and (ts - (last.get("timestamp") or 0)) <= window
            if same and close:
                clusters[-1].append(e)
                continue
        clusters.append([e])

    out: list[dict] = []
    for group in clusters:
        if len(group) == 1:
            out.append(dict(group[0]))
            continue
        amount = sum(float(g.get("token_amount") or 0) for g in group)
        value = sum(float(g.get("value_usd") or 0) for g in group)
        pnl = sum(float(g.get("closed_pnl") or 0) for g in group)
        last = group[-1]
        out.append({
            **last,
            "token_amount": amount,
            "value_usd": value,
            # 名义金额加权均价；数量为 0 时退回最后一笔的价格
            "price_usd": (value / amount) if amount else last.get("price_usd"),
            "closed_pnl": pnl or None,
            "timestamp": last.get("timestamp"),
            "count": len(group),
        })
    out.sort(key=lambda e: e.get("timestamp") or 0)  # 按时间顺序推送
    return out


async def send_telegram(tg, config, text: str) -> bool:
    """Send one HTML message via the Bot HTTP API. Returns True on success."""
    if tg is None or not config.tg_token or not config.alert_chat_id:
        return False
    url = f"https://api.telegram.org/bot{config.tg_token}/sendMessage"
    try:
        r = await tg.post(url, json={
            "chat_id": config.alert_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if r.status_code != 200:
            log.warning("telegram push failed: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - 推送失败不该影响扫描
        log.warning("telegram push error: %s", exc)
        return False


def _load_state(cursor: str | None) -> dict:
    """Parse the per-wallet cursor JSON; non-JSON (or empty) means first run."""
    try:
        d = json.loads(cursor) if cursor else {}
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


async def _wallet_events(wallet, http) -> tuple[list[dict], float]:
    """This wallet's recent Hyperliquid fills (newest-first) plus its HL total,
    which sets the tier badge shown in pushes. Keyless and unmetered."""
    addr = wallet.address
    try:
        hl = await hyperliquid_state(http, addr, with_fills=True)
    except Exception as exc:  # noqa: BLE001 - a bad round shouldn't kill the scan
        log.debug("HL scan %s failed: %s", addr, exc)
        return [], 0.0
    # 该币当前持仓的杠杆（成交记录不含历史杠杆，推送里标当前设置）。
    # 平仓后持仓里已无该币，用上次轮询记住的杠杆兜底。
    remembered = _lev_memory.setdefault(addr.lower(), {})
    for p in hl.get("positions") or []:
        if p.get("leverage"):
            remembered[p.get("coin")] = p.get("leverage")
    events = [
        {**f, "venue": f.get("venue") or "HL",
         "leverage": remembered.get(f.get("token_symbol"))}
        for f in hl.get("fills", []) or []
    ]
    events.sort(key=lambda e: e.get("timestamp") or 0, reverse=True)
    return events, float(hl.get("total_usd") or 0)


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


async def _scan_wallet(wallet, config, http, db, tg=None) -> None:
    if wallet.chain not in EVM_CHAINS:
        return await _scan_legacy(wallet, config, http, db)
    events, total = await _wallet_events(wallet, http)
    if not events:
        return

    state = _load_state(wallet.cursor)
    newest_ts = events[0].get("timestamp") or 0

    # 首轮（游标里还没有 ts）：只记录最新时间，不计数不推送，
    # 避免把整段历史算成未读、或一次性推送几十条旧成交。
    if "ts" not in state:
        db.set_cursor(wallet.id, json.dumps({"ts": newest_ts}))
        return

    last_ts = int(state.get("ts") or 0)
    fresh = [e for e in events if (e.get("timestamp") or 0) > last_ts]
    if not fresh:
        return
    db.set_cursor(wallet.id, json.dumps({"ts": max(newest_ts, last_ts)}))

    # 小额成交不计入红圈，避免刷屏
    big = [e for e in fresh if (e.get("value_usd") or 0) >= config.alert_min_usd]
    if big:
        db.bump_unread(wallet.id, len(big))  # 面板红圈：所有钱包都记

    # Telegram 推送：只推名称以 PUSH_PREFIX 开头的钱包，且先合并连续的相同动作
    if not big or tg is None or not wants_push(wallet, config):
        return
    tier = _tier(total)
    for e in merge_trades(big, config.push_merge_window):
        is_large = (e.get("value_usd") or 0) >= config.alert_large_usd
        await send_telegram(tg, config, format_trade_alert(e, wallet, tier, is_large))
        await asyncio.sleep(0.3)  # 避开 Telegram 的发送频率限制


async def scan_once(config, http, db, tg=None) -> None:
    """Walk every watched wallet once: bump unread counts, push where configured."""
    for wallet in db.list_wallets():
        try:
            await _scan_wallet(wallet, config, http, db, tg)
        except Exception as exc:  # noqa: BLE001
            log.warning("unexpected scan error %s: %s", wallet.address, exc)
        await asyncio.sleep(0.5)  # gentle pacing across wallets / APIs


def _push_ready(config) -> bool:
    return bool(config.tg_token and config.alert_chat_id)


async def run_poller(config, http, db) -> None:
    """Background loop: scan for new trades every ``POLL_INTERVAL`` seconds.

    Telegram pushes go out over a dedicated client so ``TELEGRAM_PROXY`` still
    applies (the shared client talks to HL/Moralis and must stay direct).
    """
    await asyncio.sleep(15)  # 让网页先起来，避免启动瞬间抢免费接口额度
    tg = None
    if _push_ready(config):
        import httpx
        kwargs = {"timeout": 20.0}
        if config.tg_proxy:
            kwargs["proxy"] = config.tg_proxy
        tg = httpx.AsyncClient(**kwargs)
        log.info(
            "Telegram 推送已启用：只推名称以 %r 开头的钱包（合并窗口 %ds）",
            config.push_prefix or "(全部)", config.push_merge_window,
        )
    else:
        log.info("未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，只亮面板红圈不推送。")
    try:
        while True:
            try:
                await scan_once(config, http, db, tg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let one round kill the loop
                log.warning("scan round failed: %s", exc)
            await asyncio.sleep(config.poll_interval)
    finally:
        if tg is not None:
            await tg.aclose()
