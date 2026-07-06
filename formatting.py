"""Render balances / actions / wallet lists into Telegram HTML messages."""

from __future__ import annotations

import html
from datetime import datetime

from chains.base import ACTION_EMOJI, Action, Balance
from chains.util import fmt_amount, short_addr
from db import Wallet


def _esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def _ts(unix: int) -> str:
    """Server-local time, same format as the dashboard (fmtDate) — the old UTC
    stamp made the same trade look like a different record next to the panel."""
    if not unix:
        return ""
    return datetime.fromtimestamp(unix).strftime("%Y-%m-%d %H:%M")


# --- 与网页面板同一套数字格式（fmt/usd/price 的 Python 版），保证两边显示一致 ---
def _fmt2(x) -> str:
    x = float(x or 0)
    a = abs(x)
    if a >= 1e9:
        return f"{x / 1e9:.2f}B"
    if a >= 1e6:
        return f"{x / 1e6:.2f}M"
    return f"{x:,.2f}"


def _usd2(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    sign = "-" if x < 0 else ""
    return f"{sign}${_fmt2(abs(x))}"


def _price2(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    a = abs(x)
    if a >= 1:
        return f"${x:,.2f}"
    if a == 0:
        return "$0.00"
    return f"${float(f'{x:.2g}'):g}"  # 小于 1 的价格保留 2 位有效数字，同面板


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


_DIR_CN = {
    "Open Long": "开多", "Close Long": "平多",
    "Open Short": "开空", "Close Short": "平空",
    "Buy": "买入", "Sell": "卖出",
}


def _dir_cn(d: str, typ: str) -> str:
    if d and d in _DIR_CN:
        return _DIR_CN[d]
    if d:
        return (
            d.replace("Open", "开").replace("Close", "平")
            .replace("Long", "多").replace("Short", "空")
        )
    return "买入" if typ == "BUY" else "卖出"


def format_trade_alert(e: dict, wallet: Wallet, tier: tuple[str, str],
                       is_large: bool = False) -> str:
    """Telegram push for one new trade (HL fill or EVM swap) on a watched wallet."""
    tier_emoji, tier_name = tier
    name = wallet.label or short_addr(wallet.address)
    typ = e.get("type")
    side_emoji = "🟢" if typ == "BUY" else "🔴"
    action = _dir_cn(e.get("dir") or "", typ or "")
    coin = e.get("token_symbol") or "?"
    amount = e.get("token_amount") or 0
    price = e.get("price_usd")
    value = e.get("value_usd") or 0
    venue = e.get("venue") or ""
    venue_tag = "⚡HL" if venue == "HL" else _esc(str(venue).upper())
    pnl = e.get("closed_pnl")
    url = e.get("explorer_url") or ""

    price_str = f" @ {_price2(price)}" if price else ""
    lev = e.get("leverage")
    lev_tag = f" · {lev}x" if lev else ""
    head = f"{'🔥 ' if is_large else ''}{tier_emoji} <b>{_esc(tier_name)}</b> · {_esc(name)}"
    # Only append the address chip when a custom label is set (otherwise the name
    # already is the short address and we'd show it twice).
    addr_tag = f"  <code>{_esc(short_addr(wallet.address, 6, 4))}</code>" if wallet.label else ""
    lines = [
        f"{head}{addr_tag}",
        f"{side_emoji} {_esc(action)} <b>{_esc(coin)}</b> "
        f"{_fmt2(amount)}（≈ {_usd2(value)}{price_str}）· {venue_tag}{lev_tag}",
    ]
    if pnl:
        sign = "+" if pnl >= 0 else ""
        lines.append(f"平仓盈亏 <b>{sign}{_usd2(pnl)}</b>")
    meta = []
    ts = _ts(e.get("timestamp") or 0)
    if ts:
        meta.append(ts)
    if url:
        meta.append(f'<a href="{url}">查看</a>')
    if meta:
        lines.append(f"<i>{' · '.join(meta)}</i>")
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
