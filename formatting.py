"""Render balances / actions / wallet lists into Telegram HTML messages."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from chains.base import ACTION_EMOJI, Action, Balance
from chains.util import fmt_amount, short_addr
from db import Wallet


def _esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def _ts(unix: int) -> str:
    if not unix:
        return ""
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_balance(bal: Balance, label: str = "") -> str:
    head = f"<b>{_esc(bal.chain.upper())}</b>"
    if label:
        head += f" · {_esc(label)}"
    native_line = f"余额：<b>{fmt_amount(bal.native_amount)} {_esc(bal.native_symbol)}</b>"
    if "native_usd" in bal.extra:
        native_line += f"　≈ ${fmt_amount(bal.extra['native_usd'])}"
    lines = [
        f"💼 {head}",
        f"<code>{_esc(bal.address)}</code>",
        "",
        native_line,
    ]
    if "total_usd" in bal.extra:
        lines.append(f"估算总值：<b>${fmt_amount(bal.extra['total_usd'])}</b>")
    if bal.tokens:
        lines.append("")
        lines.append("代币持仓：")
        for t in bal.tokens[:12]:
            lines.append(f"  • {fmt_amount(t.amount)} {_esc(t.symbol)}")
    if "tx_count" in bal.extra:
        lines.append("")
        lines.append(f"历史交易数：{bal.extra['tx_count']}")
    if bal.extra.get("note"):
        lines.append("")
        lines.append(f"<i>{_esc(bal.extra['note'])}</i>")
    return "\n".join(lines)


def format_action(action: Action, label: str = "") -> str:
    """A single action line, used inside lists and alerts."""
    emoji = ACTION_EMOJI.get(action.action_type, "•")
    ts = _ts(action.timestamp)
    line = f"{emoji} {_esc(action.summary)}"
    meta = []
    if ts:
        meta.append(ts)
    meta.append(f'<a href="{action.explorer_url}">查看</a>')
    return f"{line}\n   <i>{' · '.join(meta)}</i>"


def format_history(
    actions: list[Action], chain: str, address: str, label: str = ""
) -> str:
    head = f"<b>{_esc(chain.upper())}</b>"
    if label:
        head += f" · {_esc(label)}"
    head += f"\n<code>{_esc(short_addr(address, 10, 8))}</code>"
    if not actions:
        return head + "\n\n暂无可解析的交易记录。"
    body = "\n\n".join(format_action(a) for a in actions)
    return f"📜 最近 {len(actions)} 笔动作\n{head}\n\n{body}"


def format_alert(action: Action, wallet: Wallet) -> str:
    """A push notification for one new action on a tracked wallet."""
    emoji = ACTION_EMOJI.get(action.action_type, "•")
    name = wallet.label or short_addr(wallet.address)
    lines = [
        f"🔔 <b>{_esc(name)}</b> · {_esc(wallet.chain.upper())}",
        f"{emoji} {_esc(action.summary)}",
        f"<i>{_ts(action.timestamp)}</i> · "
        f'<a href="{action.explorer_url}">查看交易</a>',
        f"<code>{_esc(short_addr(wallet.address, 10, 8))}</code>",
    ]
    return "\n".join(lines)


def format_wallet_list(wallets: list[Wallet]) -> str:
    if not wallets:
        return "📭 还没有跟踪任何钱包。\n用 <code>/add &lt;链&gt; &lt;地址&gt; [备注]</code> 添加。"
    by_chain: dict[str, list[Wallet]] = {}
    for w in wallets:
        by_chain.setdefault(w.chain, []).append(w)
    lines = [f"📋 跟踪清单（共 {len(wallets)} 个）", ""]
    for chain in sorted(by_chain):
        lines.append(f"<b>{_esc(chain.upper())}</b>")
        for w in by_chain[chain]:
            label = f" · {_esc(w.label)}" if w.label else ""
            lines.append(
                f"  <code>#{w.id}</code> {_esc(short_addr(w.address, 8, 6))}{label}"
            )
        lines.append("")
    return "\n".join(lines).strip()
