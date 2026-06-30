"""Small shared helpers used by the chain clients."""

from __future__ import annotations

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    """Decode a base58 (Bitcoin/Solana alphabet) string to bytes.

    Raises ValueError on any invalid character."""
    num = 0
    for ch in s:
        idx = _B58_ALPHABET.find(ch)
        if idx == -1:
            raise ValueError(f"invalid base58 character: {ch!r}")
        num = num * 58 + idx
    # Convert the integer to bytes.
    full = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    # Each leading '1' represents a leading zero byte.
    n_pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_pad + full


def fmt_amount(x: float) -> str:
    """Format a token amount compactly for human display."""
    if x is None:
        return "0"
    ax = abs(x)
    if ax == 0:
        return "0"
    if ax >= 1_000_000:
        return f"{x:,.0f}"
    if ax >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    if ax >= 0.0001:
        return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:.3e}"


def short_addr(address: str, head: int = 6, tail: int = 4) -> str:
    if len(address) <= head + tail + 1:
        return address
    return f"{address[:head]}…{address[-tail:]}"
