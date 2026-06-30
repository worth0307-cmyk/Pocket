"""Moralis-backed wallet portfolio (token holdings + USD + spam flags).

The free Etherscan tier only exposes the native balance, which is why the
dashboard previously showed far less than DeBank. Moralis' free tier returns
the full token list *with* USD prices and a ``possible_spam`` flag, across all
major EVM chains — this module aggregates those into a DeBank-style portfolio.
Docs: https://docs.moralis.com/web3-data-api/evm
"""

from __future__ import annotations

import httpx

from .base import ChainError

MORALIS_BASE = "https://deep-index.moralis.io/api/v2.2"

# Our chain id -> Moralis chain hex.
MORALIS_CHAIN = {
    "eth": "0x1",
    "bsc": "0x38",
    "base": "0x2105",
    "arb": "0xa4b1",
    "polygon": "0x89",
    "op": "0xa",
}
# Reverse, for parsing net-worth responses (Moralis echoes chain names there).
_NAME_BY_HEX = {v: k for k, v in MORALIS_CHAIN.items()}

# Default set of EVM chains we aggregate when none is specified.
DEFAULT_EVM_CHAINS = ["eth", "bsc", "base", "arb", "polygon", "op"]


def is_supported(chain: str) -> bool:
    return chain in MORALIS_CHAIN


async def _get(http: httpx.AsyncClient, key: str, path: str, params) -> dict:
    try:
        resp = await http.get(
            f"{MORALIS_BASE}{path}",
            params=params,
            headers={"X-API-Key": key, "accept": "application/json"},
            timeout=25,
        )
        if resp.status_code == 401:
            raise ChainError("Moralis API key 无效")
        if resp.status_code == 429:
            raise ChainError("Moralis 速率限制，请稍后再试")
        resp.raise_for_status()
        return resp.json()
    except ChainError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise ChainError(f"moralis request failed: {exc}") from exc


def _amount(t: dict) -> float:
    bf = t.get("balance_formatted")
    if bf is not None:
        try:
            return float(bf)
        except (TypeError, ValueError):
            pass
    try:
        return int(t.get("balance", 0) or 0) / (10 ** int(t.get("decimals", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


async def evm_net_worth(
    http: httpx.AsyncClient, key: str, address: str, chains: list[str]
) -> dict:
    """Total USD net worth + per-chain breakdown in a single call."""
    params = [
        ("exclude_spam", "true"),
        ("exclude_unverified_contracts", "true"),
    ]
    params += [("chains[]", MORALIS_CHAIN[c]) for c in chains if c in MORALIS_CHAIN]
    data = await _get(http, key, f"/wallets/{address}/net-worth", params)
    out = []
    for c in data.get("chains", []) or []:
        raw = c.get("chain")
        name = _NAME_BY_HEX.get(raw, raw)
        out.append({"chain": name, "usd": float(c.get("networth_usd", 0) or 0)})
    out.sort(key=lambda x: x["usd"], reverse=True)
    return {
        "total_usd": float(data.get("total_networth_usd", 0) or 0),
        "chains": out,
    }


async def evm_portfolio(
    http: httpx.AsyncClient, key: str, address: str, chains: list[str]
) -> dict:
    """Aggregate token holdings (with USD + spam flags) across EVM chains."""
    tokens: list[dict] = []
    spam: set[str] = set()
    per_chain: dict[str, float] = {}
    total = 0.0

    for c in chains:
        mc = MORALIS_CHAIN.get(c)
        if not mc:
            continue
        try:
            data = await _get(
                http, key, f"/wallets/{address}/tokens",
                [("chain", mc), ("limit", "100")],
            )
        except ChainError:
            continue  # one chain failing shouldn't drop the whole portfolio
        for t in data.get("result", []) or []:
            contract = (t.get("token_address") or "").lower()
            if t.get("possible_spam"):
                if contract:
                    spam.add(contract)
                continue
            usd_value = float(t.get("usd_value") or 0)
            tokens.append({
                "chain": c,
                "symbol": t.get("symbol") or "?",
                "name": t.get("name") or "",
                "amount": _amount(t),
                "price": float(t["usd_price"]) if t.get("usd_price") else None,
                "usd_value": usd_value,
                "contract": contract,
                "native": bool(t.get("native_token")),
            })
            total += usd_value
            per_chain[c] = per_chain.get(c, 0.0) + usd_value

    tokens.sort(key=lambda x: x["usd_value"], reverse=True)
    chain_list = [
        {"chain": k, "usd": v}
        for k, v in sorted(per_chain.items(), key=lambda kv: -kv[1])
    ]
    return {
        "total_usd": total,
        "chains": chain_list,
        "tokens": tokens,
        "spam_contracts": sorted(spam),
    }
