"""Free, keyless USD prices for native coins via the CoinGecko public API.

Used to value native balances and to estimate PnL. Results are cached briefly
in-process so repeated dashboard refreshes don't hammer the rate-limited API.
"""

from __future__ import annotations

import time

import httpx

# Native symbol -> CoinGecko coin id.
COINGECKO_IDS = {
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
    "BTC": "bitcoin",
    "POL": "matic-network",
    "MATIC": "matic-network",
}

_TTL = 120  # seconds
_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price_usd, fetched_at)


async def native_usd_prices(
    http: httpx.AsyncClient, symbols: list[str]
) -> dict[str, float]:
    """Return {SYMBOL: usd_price} for the given native symbols (best effort)."""
    wanted = {s.upper() for s in symbols if s}
    now = time.time()
    out: dict[str, float] = {}
    need: set[str] = set()
    for s in wanted:
        cached = _cache.get(s)
        if cached and now - cached[1] < _TTL:
            out[s] = cached[0]
        elif s in COINGECKO_IDS:
            need.add(s)

    if need:
        ids = ",".join(sorted({COINGECKO_IDS[s] for s in need}))
        try:
            resp = await http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ids, "vs_currencies": "usd"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            for s in need:
                price = data.get(COINGECKO_IDS[s], {}).get("usd")
                if price is not None:
                    _cache[s] = (float(price), now)
                    out[s] = float(price)
        except (httpx.HTTPError, ValueError):
            pass  # prices are optional; degrade gracefully
    return out
