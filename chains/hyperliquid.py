"""Hyperliquid account state via its free, keyless public API.

Hyperliquid is a separate L1 (perps + spot DEX); a wallet's value there lives in
Hyperliquid's own account system, not as ERC-20 tokens in the EVM wallet — which
is why Moralis token balances miss it. This module pulls the perps account value
+ open positions and spot balances so the dashboard total matches DeBank.
Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
"""

from __future__ import annotations

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


async def hyperliquid_state(
    http: httpx.AsyncClient, address: str, with_fills: bool = False
) -> dict:
    """Perps account value + open positions + spot balances for an address.

    When ``with_fills`` is set, also include recent trades (buy/sell fills) so
    the action stream can show Hyperliquid activity, not just EVM DEX swaps.
    """
    address = address.strip().lower()

    perps = await _post(http, {"type": "clearinghouseState", "user": address})
    account_value = 0.0
    positions: list[dict] = []
    if isinstance(perps, dict):
        ms = perps.get("marginSummary", {}) or {}
        account_value = _f(ms.get("accountValue"))
        for ap in perps.get("assetPositions", []) or []:
            p = ap.get("position", {}) or {}
            szi = _f(p.get("szi"))
            if szi == 0:
                continue
            lev = (p.get("leverage") or {}).get("value")
            positions.append({
                "coin": p.get("coin"),
                "size": szi,
                "side": "多" if szi > 0 else "空",
                "value_usd": _f(p.get("positionValue")),
                "entry_px": _f(p.get("entryPx")) if p.get("entryPx") else None,
                "unrealized_pnl": _f(p.get("unrealizedPnl")),
                "leverage": lev,
            })

    # Spot balances (best effort; valued via mid prices where available).
    spot: list[dict] = []
    spot_usd = 0.0
    try:
        sp = await _post(http, {"type": "spotClearinghouseState", "user": address})
        mids = await _all_mids(http)
        for b in (sp or {}).get("balances", []) or []:
            coin = b.get("coin")
            total = _f(b.get("total"))
            if total <= 0:
                continue
            if coin == "USDC":
                val = total
            else:
                px = mids.get(coin)
                val = total * _f(px) if px else 0.0
            spot.append({"coin": coin, "amount": total, "usd": val})
            spot_usd += val
    except ChainError:
        pass

    positions.sort(key=lambda x: abs(x["value_usd"]), reverse=True)
    spot.sort(key=lambda x: x["usd"], reverse=True)

    fills: list = []
    if with_fills:
        try:
            fills = await _user_fills(http, address)
        except ChainError:
            pass

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
