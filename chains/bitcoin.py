"""Bitcoin client backed by the free, keyless mempool.space REST API.

Bitcoin has no notion of "buy"/"sell"; every action is a transfer in or out,
computed from the net effect of a transaction on the watched address.
Docs: https://mempool.space/docs/api/rest
"""

from __future__ import annotations

import time

import httpx

from .base import Action, ActionType, Balance, ChainClient, ChainError
from .util import fmt_amount, short_addr

API_BASE = "https://mempool.space/api"
SATS = 100_000_000


class BitcoinClient(ChainClient):
    name = "btc"
    display_name = "Bitcoin"
    native_symbol = "BTC"

    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    def is_valid_address(self, address: str) -> bool:
        a = address.strip()
        if a.startswith(("bc1", "tb1")):
            return 14 <= len(a) <= 74
        if a[:1] in ("1", "3"):
            return 26 <= len(a) <= 35
        return False

    def explorer_tx(self, txid: str) -> str:
        return f"https://mempool.space/tx/{txid}"

    async def _get(self, path: str) -> object:
        try:
            resp = await self._http.get(f"{API_BASE}{path}", timeout=20)
            if resp.status_code == 400:
                raise ChainError("无效的比特币地址")
            resp.raise_for_status()
            return resp.json()
        except ChainError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainError(f"mempool.space request failed: {exc}") from exc

    async def get_balance(self, address: str) -> Balance:
        address = address.strip()
        data = await self._get(f"/address/{address}")
        chain = data.get("chain_stats", {})
        mem = data.get("mempool_stats", {})
        funded = chain.get("funded_txo_sum", 0) + mem.get("funded_txo_sum", 0)
        spent = chain.get("spent_txo_sum", 0) + mem.get("spent_txo_sum", 0)
        btc = (funded - spent) / SATS
        tx_count = chain.get("tx_count", 0) + mem.get("tx_count", 0)
        return Balance(
            chain=self.name,
            address=address,
            native_symbol=self.native_symbol,
            native_amount=btc,
            tokens=[],
            extra={"tx_count": tx_count},
        )

    async def get_actions(self, address: str, limit: int = 20) -> list[Action]:
        address = address.strip()
        txs = await self._get(f"/address/{address}/txs")
        if not isinstance(txs, list):
            return []
        actions = [self._parse(tx, address) for tx in txs[:limit]]
        return actions

    def _parse(self, tx: dict, wallet: str) -> Action:
        txid = tx.get("txid", "")
        status = tx.get("status", {})
        ts = int(status.get("block_time") or time.time())

        # Sats the wallet spent (its inputs) vs. received (its outputs).
        spent = sum(
            vin.get("prevout", {}).get("value", 0)
            for vin in tx.get("vin", [])
            if vin.get("prevout", {}).get("scriptpubkey_address") == wallet
        )
        received = sum(
            vout.get("value", 0)
            for vout in tx.get("vout", [])
            if vout.get("scriptpubkey_address") == wallet
        )
        net = (received - spent) / SATS

        if net >= 0:
            atype = ActionType.TRANSFER_IN
            counterparty = next(
                (
                    vin.get("prevout", {}).get("scriptpubkey_address")
                    for vin in tx.get("vin", [])
                    if vin.get("prevout", {}).get("scriptpubkey_address") != wallet
                ),
                "",
            )
            summary = f"转入 {fmt_amount(net)} BTC ← {short_addr(counterparty or '?')}"
        else:
            atype = ActionType.TRANSFER_OUT
            counterparty = next(
                (
                    vout.get("scriptpubkey_address")
                    for vout in tx.get("vout", [])
                    if vout.get("scriptpubkey_address")
                    and vout.get("scriptpubkey_address") != wallet
                ),
                "",
            )
            summary = f"转出 {fmt_amount(abs(net))} BTC → {short_addr(counterparty or '?')}"

        if not status.get("confirmed", True):
            summary += "（未确认）"

        return Action(
            chain=self.name,
            address=wallet,
            tx_hash=txid,
            timestamp=ts,
            action_type=atype,
            summary=summary,
            explorer_url=self.explorer_tx(txid),
        )
