"""FastAPI dashboard (Arkham-style dark UI) over the same backend as the bot.

Runs in its own thread / event loop with its own httpx client, and shares the
thread-safe ``WalletDB`` with the Telegram bot. Read endpoints query balances
and action history on demand; write endpoints manage the watch-list.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import analytics
import chains
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
    }


class AddWallet(BaseModel):
    chain: str
    address: str
    label: str = ""


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

    @app.delete("/api/wallets/{wallet_id}")
    async def api_remove(wallet_id: int, _: None = Depends(auth)) -> dict:
        removed = db.remove_by_id(wallet_id)
        if not removed:
            raise HTTPException(status_code=404, detail="未找到")
        return {"removed": _wallet_dict(removed)}

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
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
