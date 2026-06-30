"""Hyperliquid account state via its free, keyless public API.

Hyperliquid is a separate L1 (perps + spot DEX); a wallet's value there lives in
Hyperliquid's own account system, not as ERC-20 tokens in the EVM wallet — which
is why Moralis token balances miss it. This module pulls the perps account value
+ open positions and spot balances so the dashboard total matches DeBank.
Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
"""

from __future__ import annotations

import httpx

from .base import ChainError

HL_API = "https://api.hyperliquid.xyz/info"


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


async def hyperliquid_state(http: httpx.AsyncClient, address: str) -> dict:
    """Perps account value + open positions + spot balances for an address."""
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
        mids = await _post(http, {"type": "allMids"})
        mids = mids if isinstance(mids, dict) else {}
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
    return {
        "account_value": account_value,
        "positions": positions,
        "spot": spot,
        "spot_usd": spot_usd,
        "total_usd": account_value + spot_usd,
    }
