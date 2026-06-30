"""USD valuation and a lightweight PnL estimate built on the free price feed.

The PnL is an *estimate* from the wallet's visible recent action history:
realized PnL ≈ Σ(proceeds from SELLs) − Σ(cost of BUYs), with the native-coin
legs valued at the *current* price (free history is limited). Stablecoin legs
are taken at ~$1. It is clearly labelled as an estimate in the UI.
"""

from __future__ import annotations

import httpx

from chains.base import Action, ActionType, Balance
from chains.prices import native_usd_prices

_STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "BUSD"}


async def enrich_balance_usd(balance: Balance, http: httpx.AsyncClient) -> Balance:
    """Add native USD price / value and a best-effort total USD to ``balance``."""
    prices = await native_usd_prices(http, [balance.native_symbol])
    price = prices.get(balance.native_symbol.upper())
    total = 0.0
    have_total = False
    if price is not None:
        balance.extra["native_price"] = price
        balance.extra["native_usd"] = balance.native_amount * price
        total += balance.native_amount * price
        have_total = True
    # Value recognized stablecoin balances 1:1; other tokens have no free price.
    for t in balance.tokens:
        if t.symbol.upper() in _STABLE_SYMBOLS:
            total += t.amount
            have_total = True
    if have_total:
        balance.extra["total_usd"] = total
    return balance


async def estimate_pnl(
    actions: list[Action], native_symbol: str, http: httpx.AsyncClient
) -> dict:
    """Estimate realized PnL (USD) from a list of recent actions."""
    prices = await native_usd_prices(http, [native_symbol])
    price = prices.get(native_symbol.upper())

    cost = 0.0       # USD spent on buys
    proceeds = 0.0   # USD received from sells
    buys = sells = 0
    skipped = 0      # trades whose native leg couldn't be priced

    for a in actions:
        if a.quote_kind is None or a.action_type not in (
            ActionType.BUY,
            ActionType.SELL,
        ):
            continue
        if a.quote_kind == "native":
            if price is None:
                skipped += 1
                continue
            usd = a.quote_amount * price
        else:  # stable leg ~ USD
            usd = a.quote_amount
        if a.action_type == ActionType.BUY:
            cost += usd
            buys += 1
        else:
            proceeds += usd
            sells += 1

    return {
        "realized_pnl_usd": proceeds - cost,
        "bought_usd": cost,
        "sold_usd": proceeds,
        "buys": buys,
        "sells": sells,
        "native_price": price,
        "window": len(actions),
        "skipped_unpriced": skipped,
    }
