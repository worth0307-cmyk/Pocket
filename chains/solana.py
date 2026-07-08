"""Solana client.

Balances come from a JSON-RPC endpoint (Helius RPC if a key is configured,
otherwise the public mainnet RPC). Action parsing uses the free Helius
Enhanced Transactions API, which already classifies SWAP / TRANSFER and ships
a human readable ``description`` for each transaction.
Docs: https://docs.helius.dev/
"""

from __future__ import annotations

import httpx

from .base import (
    Action,
    ActionsUnsupported,
    ActionType,
    Balance,
    ChainClient,
    ChainError,
    TokenBalance,
)
from .util import b58decode, fmt_amount, short_addr

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOL_DECIMALS = 1_000_000_000  # lamports per SOL

# Mints treated as "money" for buy/sell direction, plus friendly symbols.
KNOWN_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "So11111111111111111111111111111111111111112": "SOL",
}
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}


class SolanaClient(ChainClient):
    name = "sol"
    display_name = "Solana"
    native_symbol = "SOL"

    def __init__(self, helius_api_key: str, http: httpx.AsyncClient):
        self._key = helius_api_key
        self._http = http

    @property
    def rpc_url(self) -> str:
        if self._key:
            return f"https://mainnet.helius-rpc.com/?api-key={self._key}"
        return PUBLIC_RPC

    def is_valid_address(self, address: str) -> bool:
        address = address.strip()
        if not (32 <= len(address) <= 44):
            return False
        try:
            return len(b58decode(address)) == 32
        except ValueError:
            return False

    def explorer_tx(self, sig: str) -> str:
        return f"https://solscan.io/tx/{sig}"

    async def _rpc(self, method: str, params: list) -> object:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await self._http.post(self.rpc_url, json=body, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainError(f"solana rpc failed: {exc}") from exc
        if "error" in data:
            raise ChainError(f"solana rpc error: {data['error']}")
        return data.get("result")

    async def get_balance(self, address: str) -> Balance:
        address = address.strip()
        lamports = await self._rpc("getBalance", [address])
        sol = 0.0
        if isinstance(lamports, dict):
            sol = (lamports.get("value", 0) or 0) / SOL_DECIMALS

        tokens: list[TokenBalance] = []
        try:
            result = await self._rpc(
                "getTokenAccountsByOwner",
                [
                    address,
                    {"programId": TOKEN_PROGRAM},
                    {"encoding": "jsonParsed"},
                ],
            )
            for acc in (result or {}).get("value", []):
                info = (
                    acc.get("account", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {})
                )
                amt = info.get("tokenAmount", {}).get("uiAmount") or 0
                mint = info.get("mint", "")
                if amt and amt > 0:
                    sym = KNOWN_MINTS.get(mint, short_addr(mint))
                    tokens.append(TokenBalance(symbol=sym, amount=amt, contract=mint))
        except ChainError:
            pass  # balance for SOL still useful even if token scan fails
        tokens.sort(key=lambda t: t.amount, reverse=True)
        return Balance(
            chain=self.name,
            address=address,
            native_symbol=self.native_symbol,
            native_amount=sol,
            tokens=tokens[:12],
        )

    async def get_actions(self, address: str, limit: int = 20) -> list[Action]:
        if not self._key:
            raise ActionsUnsupported(
                "Solana 动作解析需要免费的 HELIUS_API_KEY（余额查询不受影响）"
            )
        address = address.strip()
        url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
        params = {"api-key": self._key, "limit": min(max(limit, 1), 100)}
        try:
            resp = await self._http.get(url, params=params, timeout=25)
            resp.raise_for_status()
            items = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainError(f"helius request failed: {exc}") from exc
        if not isinstance(items, list):
            return []
        return [self._parse(it, address) for it in items]

    def _parse(self, it: dict, wallet: str) -> Action:
        sig = it.get("signature", "")
        ts = int(it.get("timestamp", 0) or 0)

        # Net SOL change for the wallet (lamports).
        sol_delta = 0
        for nt in it.get("nativeTransfers", []) or []:
            amt = int(nt.get("amount", 0) or 0)
            if nt.get("toUserAccount") == wallet:
                sol_delta += amt
            if nt.get("fromUserAccount") == wallet:
                sol_delta -= amt
        sol_delta /= SOL_DECIMALS

        # Net token change per mint for the wallet.
        deltas: dict[str, float] = {}
        for tt in it.get("tokenTransfers", []) or []:
            mint = tt.get("mint", "")
            amt = float(tt.get("tokenAmount", 0) or 0)
            if tt.get("toUserAccount") == wallet:
                deltas[mint] = deltas.get(mint, 0) + amt
            if tt.get("fromUserAccount") == wallet:
                deltas[mint] = deltas.get(mint, 0) - amt

        gained = [(m, a) for m, a in deltas.items() if a > 1e-12 and m not in STABLE_MINTS]
        lost = [(m, a) for m, a in deltas.items() if a < -1e-12 and m not in STABLE_MINTS]
        stable_gain_amt = sum(a for m, a in deltas.items() if m in STABLE_MINTS and a > 0)
        stable_loss_amt = -sum(a for m, a in deltas.items() if m in STABLE_MINTS and a < 0)
        stable_gain = stable_gain_amt > 1e-12
        stable_loss = stable_loss_amt > 1e-12
        spent_value = sol_delta < -1e-9 or stable_loss
        recv_value = sol_delta > 1e-9 or stable_gain

        def sym(mint: str) -> str:
            return KNOWN_MINTS.get(mint, short_addr(mint))

        # Prefer Helius's own readable description when available.
        desc = (it.get("description") or "").strip()

        quote_kind: str | None = None
        quote_amount = 0.0
        if gained and spent_value:
            atype = ActionType.BUY
            m, a = gained[0]
            summary = desc or f"买入 {fmt_amount(a)} {sym(m)}"
            if sol_delta < -1e-9:
                quote_kind, quote_amount = "native", abs(sol_delta)
            elif stable_loss:
                quote_kind, quote_amount = "stable", stable_loss_amt
        elif lost and recv_value:
            atype = ActionType.SELL
            m, a = lost[0]
            summary = desc or f"卖出 {fmt_amount(abs(a))} {sym(m)}"
            if sol_delta > 1e-9:
                quote_kind, quote_amount = "native", sol_delta
            elif stable_gain:
                quote_kind, quote_amount = "stable", stable_gain_amt
        elif gained and lost:
            atype = ActionType.SWAP
            mi, ai = gained[0]
            mo, ao = lost[0]
            summary = desc or (
                f"兑换 {fmt_amount(abs(ao))} {sym(mo)} → {fmt_amount(ai)} {sym(mi)}"
            )
        elif lost or sol_delta < -1e-9:
            atype = ActionType.TRANSFER_OUT
            if lost:
                m, a = lost[0]
                what = f"{fmt_amount(abs(a))} {sym(m)}"
            else:
                what = f"{fmt_amount(abs(sol_delta))} SOL"
            summary = desc or f"转出 {what}"
        elif gained or sol_delta > 1e-9:
            atype = ActionType.TRANSFER_IN
            if gained:
                m, a = gained[0]
                what = f"{fmt_amount(a)} {sym(m)}"
            else:
                what = f"{fmt_amount(sol_delta)} SOL"
            summary = desc or f"转入 {what}"
        else:
            atype = ActionType.OTHER
            summary = desc or (it.get("type") or "交易")

        return Action(
            chain=self.name,
            address=wallet,
            tx_hash=sig,
            timestamp=ts,
            action_type=atype,
            summary=summary,
            explorer_url=self.explorer_tx(sig),
            quote_kind=quote_kind,
            quote_amount=quote_amount,
        )
