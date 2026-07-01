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

# Market-wide mid prices are identical for every wallet, so cache them briefly
# to avoid an extra POST on every state lookup (HL is keyless but IP-rate-limited).
_mids_cache: dict = {"data": None, "ts": 0.0}
_MIDS_TTL = 30


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


async def _user_fills(http: httpx.AsyncClient, address: str, limit: int = 200) -> list:
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
    return {
        "account_value": account_value,
        "positions": positions,
        "spot": spot,
        "spot_usd": spot_usd,
        "total_usd": account_value + spot_usd,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "fills": fills,
    }
