"""Hyperliquid account state via its free, keyless public API.

Hyperliquid is a separate L1 (perps + spot DEX); a wallet's value there lives in
Hyperliquid's own account system, not as ERC-20 tokens in the EVM wallet — which
is why Moralis token balances miss it. This module pulls the perps account value
+ open positions and spot balances so the dashboard total matches DeBank.
Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .base import ChainError

HL_API = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# Market-wide mid prices are identical for every wallet, so cache them briefly
# to avoid an extra POST on every state lookup (HL is keyless but IP-rate-limited).
_mids_cache: dict = {"data": None, "ts": 0.0}
_MIDS_TTL = 30

# The leaderboard is a large, slow-changing JSON — cache it for a while.
_lb_cache: dict = {"rows": None, "ts": 0.0}
_LB_TTL = 600  # 10 分钟
# Windows the Hyperliquid leaderboard actually exposes (no biweekly / quarter).
LB_WINDOWS = ("day", "week", "month", "allTime")


async def _post(http: httpx.AsyncClient, body: dict) -> object:
    try:
        resp = await http.post(HL_API, json=body, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ChainError(f"hyperliquid request failed: {exc}") from exc


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def _all_mids(http: httpx.AsyncClient) -> dict:
    now = time.time()
    if _mids_cache["data"] is not None and now - _mids_cache["ts"] < _MIDS_TTL:
        return _mids_cache["data"]
    mids = await _post(http, {"type": "allMids"})
    if isinstance(mids, dict):
        _mids_cache["data"] = mids
        _mids_cache["ts"] = now
        return mids
    return {}


async def _leaderboard_rows(http: httpx.AsyncClient) -> list:
    """Download + normalize the leaderboard once, then cache the processed rows
    (with every window's PnL/ROI) so subsequent sorts are just an in-memory sort."""
    now = time.time()
    if _lb_cache["rows"] is not None and now - _lb_cache["ts"] < _LB_TTL:
        return _lb_cache["rows"]
    # 榜单 JSON 较大，境内访问 stats-data.hyperliquid.xyz 偶发超时/断连，
    # 重试几次；仍失败时，若有旧缓存就返回旧数据，避免直接报错。
    data = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = await http.get(HL_LEADERBOARD, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            break
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(0.8 * (attempt + 1))
    if data is None:
        if _lb_cache["rows"] is not None:
            return _lb_cache["rows"]  # 服务旧缓存，好过报错
        raise ChainError(f"hyperliquid leaderboard failed: {last_exc}")
    out = []
    for r in data.get("leaderboardRows", []) or []:
        perf = dict(r.get("windowPerformances") or [])
        row = {
            "address": r.get("ethAddress"),
            "display_name": r.get("displayName") or "",
            "account_value": _f(r.get("accountValue")),
        }
        for k in LB_WINDOWS:
            w = perf.get(k) or {}
            row[k] = {"pnl": _f(w.get("pnl")), "roi": _f(w.get("roi"))}
        out.append(row)
    _lb_cache["rows"] = out
    _lb_cache["ts"] = now
    return out


async def leaderboard(
    http: httpx.AsyncClient,
    sort: str = "week",
    direction: str = "desc",
    limit: int = 50,
) -> list[dict]:
    """Top traders from the Hyperliquid leaderboard, with every window's PnL/ROI.

    ``sort`` is "value" (account value) or one of day/week/month/allTime (that
    window's PnL). ``direction`` is "desc" (default) or "asc".
    """
    rows = await _leaderboard_rows(http)  # processed + cached
    if sort == "value":
        keyf = lambda x: x["account_value"]
    elif sort in LB_WINDOWS:
        keyf = lambda x: x[sort]["pnl"]
    else:
        keyf = lambda x: x["week"]["pnl"]
    ranked = sorted(rows, key=keyf, reverse=(direction != "asc"))
    return ranked[: max(1, min(limit, 200))]


async def _user_fills(http: httpx.AsyncClient, address: str, limit: int = 300) -> list:
    """Recent Hyperliquid trades (fills) as normalized buy/sell actions."""
    data = await _post(http, {"type": "userFills", "user": address})
    out: list[dict] = []
    if isinstance(data, list):
        for f in data:
            px = _f(f.get("px"))
            sz = _f(f.get("sz"))
            h = f.get("hash", "") or ""
            out.append({
                "type": "BUY" if f.get("side") == "B" else "SELL",
                "venue": "HL",
                "token_symbol": f.get("coin") or "?",
                "token_amount": sz,
                "price_usd": px if px else None,
                "value_usd": px * sz,
                "closed_pnl": _f(f.get("closedPnl")),
                "dir": f.get("dir") or "",
                "timestamp": int(_f(f.get("time")) / 1000) if f.get("time") else 0,
                "tx_hash": h,
                "explorer_url": f"https://app.hyperliquid.xyz/explorer/tx/{h}" if h else "",
            })
    out.sort(key=lambda x: x["timestamp"], reverse=True)
    return out[:limit]


def _parse_clearinghouse(data, dex: str | None = None) -> tuple[float, list[dict]]:
    """Extract (account_value, positions) from a clearinghouseState response."""
    account_value = 0.0
    positions: list[dict] = []
    if isinstance(data, dict):
        ms = data.get("marginSummary", {}) or {}
        account_value = _f(ms.get("accountValue"))
        for ap in data.get("assetPositions", []) or []:
            p = ap.get("position", {}) or {}
            szi = _f(p.get("szi"))
            if szi == 0:
                continue
            coin = p.get("coin") or "?"
            if dex and ":" not in coin:  # match the "dex:COIN" naming used in fills
                coin = f"{dex}:{coin}"
            positions.append({
                "coin": coin,
                "size": szi,
                "side": "多" if szi > 0 else "空",
                "value_usd": _f(p.get("positionValue")),
                "entry_px": _f(p.get("entryPx")) if p.get("entryPx") else None,
                "unrealized_pnl": _f(p.get("unrealizedPnl")),
                "leverage": (p.get("leverage") or {}).get("value"),
            })
    return account_value, positions


async def hyperliquid_state(
    http: httpx.AsyncClient, address: str, with_fills: bool = False
) -> dict:
    """Perps account value + open positions + spot balances for an address.

    When ``with_fills`` is set, also include recent trades (buy/sell fills) so
    the action stream can show Hyperliquid activity, not just EVM DEX swaps.
    Builder-deployed perps (HIP-3) live on separate sub-dexes and are included.
    """
    address = address.strip().lower()

    # Fetch the main account, spot state, mid prices and fills concurrently.
    keys = ["main", "spot", "mids"] + (["fills"] if with_fills else [])
    coros = {
        "main": _post(http, {"type": "clearinghouseState", "user": address}),
        "spot": _post(http, {"type": "spotClearinghouseState", "user": address}),
        "mids": _all_mids(http),
    }
    if with_fills:
        coros["fills"] = _user_fills(http, address)
    res = await asyncio.gather(*[coros[k] for k in keys], return_exceptions=True)
    got = {k: (v if not isinstance(v, Exception) else None) for k, v in zip(keys, res)}

    account_value, positions = _parse_clearinghouse(got.get("main"))

    # Spot balances (best effort; valued via mid prices where available).
    spot: list[dict] = []
    spot_usd = 0.0
    mids = got.get("mids") or {}
    for b in (got.get("spot") or {}).get("balances", []) or []:
        coin = b.get("coin")
        total = _f(b.get("total"))
        if total <= 0:
            continue
        val = total if coin == "USDC" else (total * _f(mids.get(coin)) if mids.get(coin) else 0.0)
        spot.append({"coin": coin, "amount": total, "usd": val})
        spot_usd += val

    fills: list = got.get("fills") or []
    if not isinstance(fills, list):
        fills = []

    # Builder-deployed perps (HIP-3): coin looks like "xyz:GOLD". The main
    # clearinghouse omits them, so query each sub-dex seen in the fills (parallel).
    dexes = {c.split(":")[0] for f in fills if ":" in (c := f.get("token_symbol") or "")}
    if dexes:
        async def _dex(d):
            try:
                return d, await _post(
                    http, {"type": "clearinghouseState", "user": address, "dex": d}
                )
            except ChainError:
                return d, None
        for d, ch in await asyncio.gather(*[_dex(d) for d in dexes]):
            if ch is None:
                continue
            av, pos = _parse_clearinghouse(ch, dex=d)
            account_value += av
            positions += pos

    positions.sort(key=lambda x: abs(x["value_usd"]), reverse=True)
    spot.sort(key=lambda x: x["usd"], reverse=True)

    unrealized_pnl = sum(p["unrealized_pnl"] for p in positions)
    realized_pnl = sum(f["closed_pnl"] for f in fills)
    # Current mid prices for the coins we reference (so already-closed coins
    # still show a 现价), kept small by filtering to seen coins.
    seen = {p["coin"] for p in positions} | {f.get("token_symbol") for f in fills}
    mids_out = {c: _f(mids[c]) for c in seen if c and c in mids}
    return {
        "account_value": account_value,
        "positions": positions,
        "spot": spot,
        "spot_usd": spot_usd,
        "total_usd": account_value + spot_usd,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "fills": fills,
        "mids": mids_out,
    }
