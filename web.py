"""FastAPI dashboard (Arkham-style dark UI) over the same backend as the bot.

Runs in its own thread / event loop with its own httpx client, and shares the
thread-safe ``WalletDB`` with the Telegram bot. Read endpoints query balances
and action history on demand; write endpoints manage the watch-list.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import analytics
import chains
from chains import hyperliquid as hl
from chains import portfolio as pf
from chains.base import ActionsUnsupported, ChainError
from config import Config
from db import WalletDB

STATIC_DIR = Path(__file__).parent / "static"


def _action_dict(a) -> dict:
    return {
        "chain": a.chain,
        "tx_hash": a.tx_hash,
        "timestamp": a.timestamp,
        "type": a.action_type.value,
        "summary": a.summary,
        "explorer_url": a.explorer_url,
        "token_contract": a.token_contract,
    }


def _balance_dict(b) -> dict:
    return {
        "chain": b.chain,
        "address": b.address,
        "native_symbol": b.native_symbol,
        "native_amount": b.native_amount,
        "tokens": [
            {"symbol": t.symbol, "amount": t.amount, "contract": t.contract}
            for t in b.tokens
        ],
        "extra": b.extra,
    }


def _wallet_dict(w) -> dict:
    return {
        "id": w.id,
        "chain": w.chain,
        "address": w.address,
        "label": w.label,
        "added_at": w.added_at,
        "unread": getattr(w, "unread", 0) or 0,
    }


class AddWallet(BaseModel):
    chain: str
    address: str
    label: str = ""


class RenameReq(BaseModel):
    label: str = ""


class MoveReq(BaseModel):
    action: str  # up / down / top


class BatchItem(BaseModel):
    address: str
    label: str = ""


class BatchAdd(BaseModel):
    chain: str = "eth"
    items: list[BatchItem]


class AnalyzeReq(BaseModel):
    addresses: list[str]


def create_web_app(config: Config, db: WalletDB) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient()
        try:
            yield
        finally:
            await app.state.http.aclose()

    app = FastAPI(
        title="Wallet Tracker", docs_url=None, redoc_url=None, lifespan=lifespan
    )

    def auth(request: Request) -> None:
        """Optional shared-token gate (header X-Auth-Token or ?token=)."""
        if not config.web_token:
            return
        token = request.headers.get("x-auth-token") or request.query_params.get("token")
        if token != config.web_token:
            raise HTTPException(status_code=401, detail="unauthorized")

    def _client(chain: str):
        client = chains.get_client(chain, config, app.state.http)
        if client is None:
            _, reason = chains.chain_status(chain, config)
            raise HTTPException(status_code=400, detail=f"链 {chain} 不可用：{reason}")
        return client

    @app.get("/api/chains")
    async def api_chains(_: None = Depends(auth)) -> dict:
        out = []
        for cid in chains.supported_chain_ids():
            usable, reason = chains.chain_status(cid, config)
            out.append({"id": cid, "usable": usable, "reason": reason})
        return {"chains": out, "moralis": bool(config.moralis_api_key)}

    @app.get("/api/wallets")
    async def api_list(_: None = Depends(auth)) -> dict:
        return {"wallets": [_wallet_dict(w) for w in db.list_wallets()]}

    @app.post("/api/wallets")
    async def api_add(body: AddWallet, _: None = Depends(auth)) -> dict:
        chain = body.chain.lower().strip()
        client = _client(chain)
        if not client.is_valid_address(body.address):
            raise HTTPException(status_code=400, detail="地址格式不正确")
        norm = client.normalize_address(body.address)
        created, wallet = db.add_wallet(
            chain, norm, body.label.strip(), config.alert_chat_id
        )
        if created:
            try:
                actions = await client.get_actions(norm, limit=1)
                if actions:
                    db.set_cursor(wallet.id, actions[0].tx_hash)
            except (ActionsUnsupported, ChainError):
                pass
        return {"created": created, "wallet": _wallet_dict(wallet)}

    @app.delete("/api/wallets")
    async def api_clear(_: None = Depends(auth)) -> dict:
        return {"removed": db.clear()}

    @app.delete("/api/wallets/{wallet_id}")
    async def api_remove(wallet_id: int, _: None = Depends(auth)) -> dict:
        removed = db.remove_by_id(wallet_id)
        if not removed:
            raise HTTPException(status_code=404, detail="未找到")
        return {"removed": _wallet_dict(removed)}

    @app.patch("/api/wallets/{wallet_id}")
    async def api_rename(
        wallet_id: int, body: RenameReq, _: None = Depends(auth)
    ) -> dict:
        wallet = db.set_label(wallet_id, body.label.strip())
        if not wallet:
            raise HTTPException(status_code=404, detail="未找到")
        return {"wallet": _wallet_dict(wallet)}

    @app.post("/api/wallets/{wallet_id}/read")
    async def api_mark_read(wallet_id: int, _: None = Depends(auth)) -> dict:
        """查看该钱包后清零未读提醒（红圈）。"""
        db.clear_unread(wallet_id)
        return {"ok": True}

    @app.post("/api/wallets/{wallet_id}/move")
    async def api_move(
        wallet_id: int, body: MoveReq, _: None = Depends(auth)
    ) -> dict:
        if body.action not in ("up", "down", "top"):
            raise HTTPException(status_code=400, detail="action 须为 up/down/top")
        return {"moved": db.move_wallet(wallet_id, body.action)}

    @app.get("/api/balance")
    async def api_balance(
        chain: str, address: str, _: None = Depends(auth)
    ) -> dict:
        client = _client(chain.lower())
        if not client.is_valid_address(address):
            raise HTTPException(status_code=400, detail="地址格式不正确")
        try:
            bal = await client.get_balance(address)
        except ChainError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        await analytics.enrich_balance_usd(bal, app.state.http)
        return _balance_dict(bal)

    @app.get("/api/pnl")
    async def api_pnl(
        chain: str,
        address: str,
        limit: int = Query(50, ge=1, le=100),
        _: None = Depends(auth),
    ) -> dict:
        client = _client(chain.lower())
        if not client.is_valid_address(address):
            raise HTTPException(status_code=400, detail="地址格式不正确")
        try:
            actions = await client.get_actions(address, limit=limit)
        except ActionsUnsupported as exc:
            return JSONResponse({"available": False, "note": str(exc)})
        except ChainError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        result = await analytics.estimate_pnl(
            actions, client.native_symbol, app.state.http
        )
        result["available"] = True
        return result

    @app.get("/api/history")
    async def api_history(
        chain: str,
        address: str,
        limit: int = Query(15, ge=1, le=50),
        _: None = Depends(auth),
    ) -> dict:
        client = _client(chain.lower())
        if not client.is_valid_address(address):
            raise HTTPException(status_code=400, detail="地址格式不正确")
        try:
            actions = await client.get_actions(address, limit=limit)
        except ActionsUnsupported as exc:
            return JSONResponse(
                {"actions": [], "note": str(exc)}, status_code=200
            )
        except ChainError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        # Attach a USD value to each BUY/SELL leg using the current native price.
        prices = await analytics.native_usd_prices(
            app.state.http, [client.native_symbol]
        )
        price = prices.get(client.native_symbol.upper())
        out = []
        for a in actions:
            d = _action_dict(a)
            d["value_usd"] = analytics.quote_value_usd(a, price)
            out.append(d)
        return {"actions": out}

    def _evm_chains(chains_param: str | None) -> list[str]:
        if chains_param:
            req = [c.strip().lower() for c in chains_param.split(",") if c.strip()]
            return [c for c in req if pf.is_supported(c)] or pf.DEFAULT_EVM_CHAINS
        return pf.DEFAULT_EVM_CHAINS

    def _require_moralis(address: str) -> None:
        if not config.moralis_api_key:
            raise HTTPException(
                status_code=400, detail="未配置 MORALIS_API_KEY（仅影响代币持仓/多链聚合）"
            )
        from chains.evm import _ADDR_RE  # reuse EVM address validation
        if not _ADDR_RE.match(address.strip()):
            raise HTTPException(status_code=400, detail="地址格式不正确")

    @app.get("/api/networth")
    async def api_networth(
        address: str, chains: str | None = None, _: None = Depends(auth)
    ) -> dict:
        _require_moralis(address)
        try:
            return await pf.evm_net_worth(
                app.state.http, config.moralis_api_key,
                address.strip().lower(), _evm_chains(chains),
            )
        except ChainError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/api/portfolio")
    async def api_portfolio(
        address: str, chains: str | None = None, _: None = Depends(auth)
    ) -> dict:
        _require_moralis(address)
        try:
            return await pf.evm_portfolio(
                app.state.http, config.moralis_api_key,
                address.strip().lower(), _evm_chains(chains),
            )
        except ChainError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/api/analyze")
    async def api_analyze(body: AnalyzeReq, _: None = Depends(auth)) -> dict:
        """Aggregate the Hyperliquid positions of several wallets by coin, to
        gauge collective direction (long/short), avg entry, conflicts, PnL."""
        from chains.evm import _ADDR_RE
        addrs = [a.strip() for a in body.addresses[:50] if _ADDR_RE.match(a.strip())]
        if not addrs:
            raise HTTPException(status_code=400, detail="没有有效的 EVM/HL 地址")
        # with_fills=True so builder-deployed perps (股票/商品，HIP-3 子交易所，
        # 形如 dex:GOLD) also get discovered and counted, not just main crypto.
        # 限并发：每个钱包内部还有 4+ 个请求，几十个钱包全放会打爆 HL 的 IP 限流。
        sem = asyncio.Semaphore(8)

        async def _state(a: str):
            async with sem:
                return await hl.hyperliquid_state(app.state.http, a, with_fills=True)

        states = await asyncio.gather(
            *[_state(a) for a in addrs], return_exceptions=True
        )

        def leg():
            return {"n": 0, "notional": 0.0, "size": 0.0, "sxe": 0.0, "upnl": 0.0}

        coins: dict = {}
        price_by_coin: dict = {}   # 现价：优先用持仓的标记价(仓位价值/数量)
        mids_all: dict = {}        # 盘口中间价兜底
        total_acct = total_upnl = 0.0
        with_pos = 0
        for st in states:
            if not isinstance(st, dict):
                continue
            total_acct += st.get("account_value", 0) or 0
            for k, v in (st.get("mids") or {}).items():
                if v:
                    mids_all[k] = v
            positions = st.get("positions") or []
            if positions:
                with_pos += 1
            for p in positions:
                c = p.get("coin") or "?"
                g = coins.setdefault(c, {"long": leg(), "short": leg()})
                lg = g["long"] if p.get("side") == "多" else g["short"]
                sz = abs(p.get("size") or 0)
                val = abs(p.get("value_usd") or 0)
                if c not in price_by_coin and sz and val:
                    price_by_coin[c] = val / sz  # 标记价 = 仓位价值 / 仓位数量
                lg["n"] += 1
                lg["notional"] += val
                lg["size"] += sz
                if p.get("entry_px"):
                    lg["sxe"] += p["entry_px"] * sz
                up = p.get("unrealized_pnl") or 0
                lg["upnl"] += up
                total_upnl += up

        out = []
        for c, g in coins.items():
            L, S = g["long"], g["short"]
            out.append({
                "coin": c,
                "cur_price": price_by_coin.get(c) or mids_all.get(c) or None,
                "long_wallets": L["n"], "long_notional": L["notional"],
                "long_avg": (L["sxe"] / L["size"]) if L["size"] else None,
                "short_wallets": S["n"], "short_notional": S["notional"],
                "short_avg": (S["sxe"] / S["size"]) if S["size"] else None,
                "net_notional": L["notional"] - S["notional"],
                "bias": ("一致做多" if S["n"] == 0 else
                         "一致做空" if L["n"] == 0 else "多空分歧"),
                "upnl": L["upnl"] + S["upnl"],
            })
        out.sort(key=lambda x: x["long_notional"] + x["short_notional"], reverse=True)
        return {
            "wallets": len(addrs), "with_positions": with_pos,
            "total_account_value": total_acct, "total_upnl": total_upnl,
            "coins": out,
        }

    @app.get("/api/hl_leaderboard")
    async def api_hl_leaderboard(
        sort: str = "week",
        dir: str = "desc",
        limit: int = Query(50, ge=1, le=200),
        _: None = Depends(auth),
    ) -> dict:
        try:
            rows = await hl.leaderboard(
                app.state.http, sort=sort, direction=dir, limit=limit
            )
        except ChainError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"rows": rows, "sort": sort, "dir": dir}

    @app.post("/api/wallets/batch")
    async def api_batch(body: BatchAdd, _: None = Depends(auth)) -> dict:
        from chains.evm import _ADDR_RE, is_evm_chain
        chain = (body.chain or "eth").lower()
        # 按链校验（此前统一用 EVM 正则，导致 sol/btc 批量导入全被判无效）。
        # EVM 用正则即可（导入 HL 排行榜地址不应依赖 Etherscan key）。
        if is_evm_chain(chain):
            def _ok(a: str) -> bool: return bool(_ADDR_RE.match(a))
            def _norm(a: str) -> str: return a.lower()
        else:
            client = _client(chain)
            _ok, _norm = client.is_valid_address, client.normalize_address
        added = skipped = invalid = 0
        # 导入的地址整体置顶，且保持文件顺序（第一条排最上）：
        # 排序键从 now 逐条递减；已在清单里的也一并上浮。
        import time as _t
        base = int(_t.time())
        for idx, it in enumerate(body.items[:200]):
            a = (it.address or "").strip()
            if not _ok(a):
                invalid += 1
                continue
            ts = base - idx
            created, w = db.add_wallet(
                chain, _norm(a), (it.label or "").strip(), config.alert_chat_id,
                sort_ts=ts,
            )
            if created:
                added += 1
            else:
                skipped += 1
                db.set_sort_ts(w.id, ts)  # 已存在的导入项也提到顶部
        return {"added": added, "skipped": skipped, "invalid": invalid}

    @app.get("/api/hyperliquid")
    async def api_hyperliquid(
        address: str, fills: int = 0, _: None = Depends(auth)
    ) -> dict:
        from chains.evm import _ADDR_RE
        if not _ADDR_RE.match(address.strip()):
            raise HTTPException(status_code=400, detail="地址格式不正确")
        try:
            return await hl.hyperliquid_state(
                app.state.http, address, with_fills=bool(fills)
            )
        except ChainError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/api/swaps")
    async def api_swaps(
        address: str,
        chains: str | None = None,
        limit: int = Query(40, ge=1, le=100),
        _: None = Depends(auth),
    ) -> dict:
        _require_moralis(address)
        try:
            return await pf.evm_swaps(
                app.state.http, config.moralis_api_key,
                address.strip().lower(), _evm_chains(chains), limit,
            )
        except ChainError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/")
    async def index() -> FileResponse:
        # no-store：更新代码后普通 F5 即可生效，不再需要 Ctrl+Shift+R 强刷
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
