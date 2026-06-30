"""Chain registry: map a chain id (e.g. "eth", "sol", "btc") to a client.

Which chains are usable depends on the configured free API keys:
  * EVM chains   -> need ETHERSCAN_API_KEY
  * Solana       -> balance works keyless; action tracking needs HELIUS_API_KEY
  * Bitcoin      -> always available (keyless mempool.space)
"""

from __future__ import annotations

from typing import Optional

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
from .bitcoin import BitcoinClient
from .evm import EVM_CHAINS, EVMClient, is_evm_chain
from .solana import SolanaClient

__all__ = [
    "Action",
    "ActionType",
    "ActionsUnsupported",
    "Balance",
    "ChainClient",
    "ChainError",
    "TokenBalance",
    "get_client",
    "chain_status",
    "supported_chain_ids",
    "EVM_CHAINS",
]


def supported_chain_ids() -> list[str]:
    return list(EVM_CHAINS.keys()) + ["sol", "btc"]


def chain_status(chain: str, config) -> tuple[bool, str]:
    """Return (usable, human reason). ``usable`` means at least balance works."""
    chain = chain.lower()
    if is_evm_chain(chain):
        if not config.etherscan_api_key:
            return False, "需要免费的 ETHERSCAN_API_KEY"
        return True, ""
    if chain == "sol":
        if not config.helius_api_key:
            return True, "余额可用；动作跟踪需 HELIUS_API_KEY"
        return True, ""
    if chain == "btc":
        return True, ""
    return False, "不支持的链"


def get_client(
    chain: str, config, http: httpx.AsyncClient
) -> Optional[ChainClient]:
    """Build a client for ``chain`` or return None if unsupported/unconfigured."""
    chain = chain.lower()
    if is_evm_chain(chain):
        if not config.etherscan_api_key:
            return None
        return EVMClient(chain, config.etherscan_api_key, http)
    if chain == "sol":
        return SolanaClient(config.helius_api_key, http)
    if chain == "btc":
        return BitcoinClient(http)
    return None
