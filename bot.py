"""Telegram command handlers for the wallet tracker bot."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import analytics
import chains
from chains.base import ActionsUnsupported, ChainError
from formatting import (
    format_balance,
    format_history,
    format_wallet_list,
)

log = logging.getLogger("bot")

HELP_TEXT = (
    "🛰 <b>钱包跟踪器</b>\n"
    "跟踪 EVM / Solana / 比特币 钱包的买入、卖出、转账，并自动推送提醒。\n\n"
    "<b>命令</b>\n"
    "/add &lt;链&gt; &lt;地址&gt; [备注] — 添加跟踪\n"
    "/remove &lt;编号&gt; 或 &lt;链&gt; &lt;地址&gt; — 移除\n"
    "/list — 查看跟踪清单\n"
    "/balance &lt;编号&gt; 或 &lt;链&gt; &lt;地址&gt; — 查余额\n"
    "/history &lt;编号&gt; 或 &lt;链&gt; &lt;地址&gt; [条数] — 查动作\n"
    "/chains — 查看支持的链与状态\n"
    "/help — 帮助\n\n"
    "<b>链代号</b>：eth, bsc, base, arb, polygon, op, sol, btc\n"
    "示例：<code>/add sol 9xQeWv...A1b2 聪明钱1号</code>"
)


def _authorized(update: Update, config) -> bool:
    if not config.allowed_chat_ids:
        return True  # no allow-list configured -> open
    chat = update.effective_chat
    return chat is not None and str(chat.id) in config.allowed_chat_ids


async def _deny(update: Update) -> None:
    await update.effective_message.reply_text("⛔️ 你没有权限使用这个机器人。")


def _resolve_target(args: list[str], db):
    """Parse command args into (chain, address, label, error).

    Accepts either a wallet id (e.g. ``#3`` / ``3``) or ``<chain> <address>``.
    """
    if not args:
        return None, None, "", "缺少参数。"
    first = args[0].lstrip("#")
    if first.isdigit():
        w = db.get_by_id(int(first))
        if not w:
            return None, None, "", f"找不到编号 #{first} 的钱包。"
        return w.chain, w.address, w.label, None
    if len(args) < 2:
        return None, None, "", "请提供 <链> <地址>。"
    return args[0].lower(), args[1], " ".join(args[2:]), None


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    if not _authorized(update, config):
        return await _deny(update)
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text(
        HELP_TEXT + f"\n\n你的 Chat ID：<code>{chat_id}</code>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    if not _authorized(update, config):
        return await _deny(update)
    await update.effective_message.reply_text(
        HELP_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_chains(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    if not _authorized(update, config):
        return await _deny(update)
    lines = ["<b>支持的链</b>"]
    for cid in chains.supported_chain_ids():
        usable, reason = chains.chain_status(cid, config)
        mark = "✅" if usable and not reason else ("⚠️" if usable else "❌")
        tail = f" — {reason}" if reason else ""
        lines.append(f"{mark} <code>{cid}</code>{tail}")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    http = context.application.bot_data["http"]
    if not _authorized(update, config):
        return await _deny(update)

    chain, address, label, err = _resolve_target(context.args, db)
    if err and not (chain and address):
        return await update.effective_message.reply_text(
            "用法：<code>/add &lt;链&gt; &lt;地址&gt; [备注]</code>\n"
            "链代号：eth, bsc, base, arb, polygon, op, sol, btc",
            parse_mode=ParseMode.HTML,
        )

    client = chains.get_client(chain, config, http)
    if client is None:
        usable, reason = chains.chain_status(chain, config)
        return await update.effective_message.reply_text(
            f"链 <code>{chain}</code> 不可用：{reason or '不支持'}",
            parse_mode=ParseMode.HTML,
        )
    if not client.is_valid_address(address):
        return await update.effective_message.reply_text(
            f"地址格式不正确（{chain}）：<code>{address}</code>",
            parse_mode=ParseMode.HTML,
        )

    norm = client.normalize_address(address)
    chat_id = str(update.effective_chat.id)
    created, wallet = db.add_wallet(chain, norm, label, chat_id)
    if not created:
        return await update.effective_message.reply_text(
            f"已在跟踪中（编号 #{wallet.id}）。"
        )

    # Seed the cursor with the latest tx so we don't replay full history later.
    try:
        actions = await client.get_actions(norm, limit=1)
        if actions:
            db.set_cursor(wallet.id, actions[0].tx_hash)
    except (ActionsUnsupported, ChainError):
        pass

    note = ""
    if chain == "sol" and not config.helius_api_key:
        note = "\n⚠️ 未配置 HELIUS_API_KEY，Solana 自动跟踪暂不可用（余额可查）。"
    await update.effective_message.reply_text(
        f"✅ 已添加 <b>{chain.upper()}</b> 钱包（编号 #{wallet.id}）"
        f"{(' · ' + label) if label else ''}{note}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    if not _authorized(update, config):
        return await _deny(update)
    if not context.args:
        return await update.effective_message.reply_text(
            "用法：<code>/remove &lt;编号&gt;</code> 或 "
            "<code>/remove &lt;链&gt; &lt;地址&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
    first = context.args[0].lstrip("#")
    if first.isdigit():
        removed = db.remove_by_id(int(first))
    elif len(context.args) >= 2:
        chain = context.args[0].lower()
        client = chains.get_client(chain, config, context.application.bot_data["http"])
        addr = client.normalize_address(context.args[1]) if client else context.args[1]
        removed = db.remove(chain, addr)
    else:
        removed = None
    if not removed:
        return await update.effective_message.reply_text("没找到要移除的钱包。")
    await update.effective_message.reply_text(
        f"🗑 已移除 <b>{removed.chain.upper()}</b> #{removed.id}。",
        parse_mode=ParseMode.HTML,
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    if not _authorized(update, config):
        return await _deny(update)
    await update.effective_message.reply_text(
        format_wallet_list(db.list_wallets()),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    http = context.application.bot_data["http"]
    if not _authorized(update, config):
        return await _deny(update)

    chain, address, label, err = _resolve_target(context.args, db)
    if not (chain and address):
        return await update.effective_message.reply_text(
            err or "用法：<code>/balance &lt;编号&gt;</code> 或 "
            "<code>/balance &lt;链&gt; &lt;地址&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
    client = chains.get_client(chain, config, http)
    if client is None:
        _, reason = chains.chain_status(chain, config)
        return await update.effective_message.reply_text(
            f"链 <code>{chain}</code> 不可用：{reason or '不支持'}",
            parse_mode=ParseMode.HTML,
        )
    if not client.is_valid_address(address):
        return await update.effective_message.reply_text("地址格式不正确。")
    msg = await update.effective_message.reply_text("查询中…")
    try:
        bal = await client.get_balance(address)
    except ChainError as exc:
        return await msg.edit_text(f"查询失败：{exc}")
    await analytics.enrich_balance_usd(bal, http)
    await msg.edit_text(
        format_balance(bal, label),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.application.bot_data["config"]
    db = context.application.bot_data["db"]
    http = context.application.bot_data["http"]
    if not _authorized(update, config):
        return await _deny(update)

    # Allow a trailing count: /history <id> 15  or  /history eth 0x.. 15
    args = list(context.args)
    limit = 8
    if args and args[-1].isdigit() and (len(args) >= 2):
        limit = max(1, min(int(args[-1]), 25))
        args = args[:-1]
    chain, address, label, err = _resolve_target(args, db)
    if not (chain and address):
        return await update.effective_message.reply_text(
            err or "用法：<code>/history &lt;编号&gt; [条数]</code>",
            parse_mode=ParseMode.HTML,
        )
    client = chains.get_client(chain, config, http)
    if client is None:
        _, reason = chains.chain_status(chain, config)
        return await update.effective_message.reply_text(
            f"链 <code>{chain}</code> 不可用：{reason or '不支持'}",
            parse_mode=ParseMode.HTML,
        )
    if not client.is_valid_address(address):
        return await update.effective_message.reply_text("地址格式不正确。")
    msg = await update.effective_message.reply_text("查询中…")
    try:
        actions = await client.get_actions(address, limit=limit)
    except ActionsUnsupported as exc:
        return await msg.edit_text(f"⚠️ {exc}")
    except ChainError as exc:
        return await msg.edit_text(f"查询失败：{exc}")
    await msg.edit_text(
        format_history(actions, chain, address, label),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=context.error)
