"""Chain-agnostic data models and the client interface every chain implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ActionType(str, Enum):
    """Normalized wallet action categories across every supported chain."""

    BUY = "BUY"
    SELL = "SELL"
    SWAP = "SWAP"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    OTHER = "OTHER"


# Emoji used when rendering an action to Telegram.
ACTION_EMOJI = {
    ActionType.BUY: "🟢",
    ActionType.SELL: "🔴",
    ActionType.SWAP: "🔁",
    ActionType.TRANSFER_IN: "📥",
    ActionType.TRANSFER_OUT: "📤",
    ActionType.OTHER: "⚪️",
}

ACTION_LABEL_CN = {
    ActionType.BUY: "买入",
    ActionType.SELL: "卖出",
    ActionType.SWAP: "兑换",
    ActionType.TRANSFER_IN: "转入",
    ActionType.TRANSFER_OUT: "转出",
    ActionType.OTHER: "其他",
}


@dataclass
class Action:
    """A single normalized wallet action (one on-chain transaction)."""

    chain: str
    address: str
    tx_hash: str
    timestamp: int  # unix seconds
    action_type: ActionType
    summary: str  # human readable one-liner (no emoji/prefix)
    explorer_url: str
    # Optional structured "money leg" of a BUY/SELL, used for PnL estimation.
    # For BUY it is what was spent; for SELL it is what was received.
    quote_kind: Optional[str] = None   # "native" | "stable" | None
    quote_amount: float = 0.0          # native coin units, or USD for stable
    token_contract: Optional[str] = None  # primary token contract (for spam filtering)


@dataclass
class TokenBalance:
    symbol: str
    amount: float
    contract: Optional[str] = None


@dataclass
class Balance:
    chain: str
    address: str
    native_symbol: str
    native_amount: float
    tokens: list[TokenBalance] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # e.g. tx_count, note


class ChainClient:
    """Interface implemented by every chain backend.

    Concrete clients are cheap, stateless wrappers around an ``httpx.AsyncClient``
    and the app ``Config``; construct them on demand via ``chains.get_client``.
    """

    name: str = ""          # short id used in commands, e.g. "eth", "sol", "btc"
    display_name: str = ""  # human name, e.g. "Ethereum"
    native_symbol: str = ""

    def normalize_address(self, address: str) -> str:
        return address.strip()

    def is_valid_address(self, address: str) -> bool:  # pragma: no cover - overridden
        return bool(address)

    async def get_balance(self, address: str) -> Balance:  # pragma: no cover
        raise NotImplementedError

    async def get_actions(
        self, address: str, limit: int = 20
    ) -> list[Action]:  # pragma: no cover
        """Return recent actions newest-first. May be empty.

        Raises ``ActionsUnsupported`` if this backend cannot parse actions
        (e.g. Solana without a Helius key)."""
        raise NotImplementedError


class ActionsUnsupported(Exception):
    """Raised when a chain client cannot return actions in the current config."""


class ChainError(Exception):
    """A recoverable error talking to a chain data source (network / API)."""
