"""EVM-chain client backed by the free Etherscan V2 multichain API.

A single Etherscan API key works across all supported chains via the
``chainid`` query parameter, so one free key covers ETH, BSC, Base, etc.
Docs: https://docs.etherscan.io/etherscan-v2
"""

from __future__ import annotations

import re
from typing import Optional

import httpx

from .base import (
    Action,
    ActionType,
    Balance,
    ChainClient,
    ChainError,
    TokenBalance,
)
from .util import fmt_amount, short_addr

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


# Per-chain metadata. ``stables`` are lowercase contract addresses treated as
# "money" (a leg of value) when deciding buy vs. sell vs. swap.
EVM_CHAINS: dict[str, dict] = {
    "eth": {
        "chainid": 1,
        "name": "Ethereum",
        "symbol": "ETH",
        "explorer": "https://etherscan.io",
        "stables": {
            "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
            "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        },
    },
    "bsc": {
        "chainid": 56,
        "name": "BNB Chain",
        "symbol": "BNB",
        "explorer": "https://bscscan.com",
        "stables": {
            "0x55d398326f99059ff775485246999027b3197955",  # USDT
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
            "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
        },
    },
    "base": {
        "chainid": 8453,
        "name": "Base",
        "symbol": "ETH",
        "explorer": "https://basescan.org",
        "stables": {
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT
        },
    },
    "arb": {
        "chainid": 42161,
        "name": "Arbitrum",
        "symbol": "ETH",
        "explorer": "https://arbiscan.io",
        "stables": {
            "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
            "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
        },
    },
    "polygon": {
        "chainid": 137,
        "name": "Polygon",
        "symbol": "POL",
        "explorer": "https://polygonscan.com",
        "stables": {
            "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",  # USDT
            "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # USDC
        },
    },
    "op": {
        "chainid": 10,
        "name": "Optimism",
        "symbol": "ETH",
        "explorer": "https://optimistic.etherscan.io",
        "stables": {
            "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58",  # USDT
            "0x0b2c639c533813f4aa9d7837caf62653d097ff85",  # USDC
        },
    },
}


def is_evm_chain(chain: str) -> bool:
    return chain in EVM_CHAINS


class EVMClient(ChainClient):
    def __init__(self, chain: str, api_key: str, http: httpx.AsyncClient):
        if chain not in EVM_CHAINS:
            raise ValueError(f"unsupported EVM chain: {chain}")
        meta = EVM_CHAINS[chain]
        self.name = chain
        self.display_name = meta["name"]
        self.native_symbol = meta["symbol"]
        self.chainid = meta["chainid"]
        self.explorer = meta["explorer"]
        self.stables = meta["stables"]
        self._api_key = api_key
        self._http = http

    def normalize_address(self, address: str) -> str:
        return address.strip().lower()

    def is_valid_address(self, address: str) -> bool:
        return bool(_ADDR_RE.match(address.strip()))

    def explorer_tx(self, tx_hash: str) -> str:
        return f"{self.explorer}/tx/{tx_hash}"

    async def _call(self, module: str, action: str, **params) -> object:
        query = {
            "chainid": self.chainid,
            "module": module,
            "action": action,
            "apikey": self._api_key,
            **params,
        }
        try:
            resp = await self._http.get(ETHERSCAN_V2_URL, params=query, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainError(f"etherscan request failed: {exc}") from exc
        status = str(data.get("status"))
        if status == "1":
            return data.get("result")
        message = str(data.get("message", ""))
        result = data.get("result")
        # "No transactions found" / "No records found" are empty, not errors.
        if "No transactions found" in message or "No records found" in message:
            return []
        if "rate limit" in str(result).lower() or "Max rate" in message:
            raise ChainError("etherscan rate limit hit, try again shortly")
        if "Invalid API Key" in str(result):
            raise ChainError("invalid Etherscan API key")
        return [] if isinstance(result, list) else result

    async def get_balance(self, address: str) -> Balance:
        address = self.normalize_address(address)
        wei = await self._call("account", "balance", address=address, tag="latest")
        try:
            native = int(wei) / 1e18
        except (TypeError, ValueError):
            native = 0.0
        note = (
            "代币持仓需付费接口，免费档仅显示原生币余额；"
            "用 /history 查看代币买卖记录。"
        )
        return Balance(
            chain=self.name,
            address=address,
            native_symbol=self.native_symbol,
            native_amount=native,
            tokens=[],
            extra={"note": note},
        )

    async def get_actions(self, address: str, limit: int = 20) -> list[Action]:
        address = self.normalize_address(address)
        offset = max(limit * 2, 30)
        normal = await self._call(
            "account", "txlist", address=address,
            page=1, offset=offset, sort="desc",
        )
        tokens = await self._call(
            "account", "tokentx", address=address,
            page=1, offset=offset, sort="desc",
        )
        normal = normal if isinstance(normal, list) else []
        tokens = tokens if isinstance(tokens, list) else []

        # Group every transfer by tx hash.
        groups: dict[str, dict] = {}
        for row in normal:
            h = row.get("hash")
            if not h:
                continue
            g = groups.setdefault(h, {"ts": 0, "native": None, "tokens": []})
            g["native"] = row
            g["ts"] = max(g["ts"], int(row.get("timeStamp", 0) or 0))
        for row in tokens:
            h = row.get("hash")
            if not h:
                continue
            g = groups.setdefault(h, {"ts": 0, "native": None, "tokens": []})
            g["tokens"].append(row)
            g["ts"] = max(g["ts"], int(row.get("timeStamp", 0) or 0))

        ordered = sorted(groups.items(), key=lambda kv: kv[1]["ts"], reverse=True)
        actions = [
            self._classify(h, g, address) for h, g in ordered[:limit]
        ]
        return actions

    def _classify(self, tx_hash: str, group: dict, wallet: str) -> Action:
        native_in = native_out = 0
        tx = group.get("native")
        if tx:
            try:
                val = int(tx.get("value", 0) or 0)
            except ValueError:
                val = 0
            if (tx.get("from") or "").lower() == wallet:
                native_out += val
            if (tx.get("to") or "").lower() == wallet:
                native_in += val
        native_in /= 1e18
        native_out /= 1e18

        tokens_in: list[tuple] = []   # (symbol, amount, contract, is_stable)
        tokens_out: list[tuple] = []
        for tr in group["tokens"]:
            contract = (tr.get("contractAddress") or "").lower()
            try:
                dec = int(tr.get("tokenDecimal") or 0)
                amt = int(tr.get("value", 0) or 0) / (10 ** dec)
            except ValueError:
                amt = 0.0
            sym = tr.get("tokenSymbol") or "?"
            is_stable = contract in self.stables
            if (tr.get("to") or "").lower() == wallet:
                tokens_in.append((sym, amt, contract, is_stable))
            if (tr.get("from") or "").lower() == wallet:
                tokens_out.append((sym, amt, contract, is_stable))

        nonstable_in = [t for t in tokens_in if not t[3]]
        nonstable_out = [t for t in tokens_out if not t[3]]
        stable_in = [t for t in tokens_in if t[3]]
        stable_out = [t for t in tokens_out if t[3]]
        spent_value = native_out > 0 or bool(stable_out)
        recv_value = native_in > 0 or bool(stable_in)

        def value_leg(is_in: bool) -> str:
            if is_in:
                if native_in > 0:
                    return f"{fmt_amount(native_in)} {self.native_symbol}"
                if stable_in:
                    return f"{fmt_amount(stable_in[0][1])} {stable_in[0][0]}"
            else:
                if native_out > 0:
                    return f"{fmt_amount(native_out)} {self.native_symbol}"
                if stable_out:
                    return f"{fmt_amount(stable_out[0][1])} {stable_out[0][0]}"
            return ""

        if nonstable_in and spent_value:
            tok = nonstable_in[0]
            atype = ActionType.BUY
            summary = f"买入 {fmt_amount(tok[1])} {tok[0]}（花费 {value_leg(False)}）"
        elif nonstable_out and recv_value:
            tok = nonstable_out[0]
            atype = ActionType.SELL
            summary = f"卖出 {fmt_amount(tok[1])} {tok[0]}（换得 {value_leg(True)}）"
        elif nonstable_in and nonstable_out:
            ti, to = nonstable_in[0], nonstable_out[0]
            atype = ActionType.SWAP
            summary = (
                f"兑换 {fmt_amount(to[1])} {to[0]} → {fmt_amount(ti[1])} {ti[0]}"
            )
        elif (native_out > 0 or tokens_out) and not (native_in > 0 or tokens_in):
            atype = ActionType.TRANSFER_OUT
            if tokens_out:
                t = tokens_out[0]
                what = f"{fmt_amount(t[1])} {t[0]}"
            else:
                what = f"{fmt_amount(native_out)} {self.native_symbol}"
            to_addr = (tx.get("to") if tx else None) or (
                group["tokens"][0].get("to") if group["tokens"] else ""
            )
            summary = f"转出 {what} → {short_addr(to_addr or '')}"
        elif (native_in > 0 or tokens_in) and not (native_out > 0 or tokens_out):
            atype = ActionType.TRANSFER_IN
            if tokens_in:
                t = tokens_in[0]
                what = f"{fmt_amount(t[1])} {t[0]}"
            else:
                what = f"{fmt_amount(native_in)} {self.native_symbol}"
            from_addr = (tx.get("from") if tx else None) or (
                group["tokens"][0].get("from") if group["tokens"] else ""
            )
            summary = f"转入 {what} ← {short_addr(from_addr or '')}"
        else:
            atype = ActionType.OTHER
            fn = (tx.get("functionName") if tx else "") or ""
            fn = fn.split("(")[0] if fn else ""
            summary = f"合约交互{(' ' + fn) if fn else ''}".strip()

        return Action(
            chain=self.name,
            address=wallet,
            tx_hash=tx_hash,
            timestamp=group["ts"],
            action_type=atype,
            summary=summary,
            explorer_url=self.explorer_tx(tx_hash),
        )
