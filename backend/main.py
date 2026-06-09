import asyncio
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def _load_dotenv():
    """Charge un fichier .env local dans os.environ s'il existe (avant les imports
    qui lisent l'environnement). Sur Render, les variables viennent du dashboard et
    aucun .env n'est présent (il est gitignoré) -> no-op. Les vraies variables
    d'environnement ont toujours priorité (setdefault)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for p in (root / ".env", root / "backend" / ".env"):
        try:
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if v.strip():
                            os.environ.setdefault(k.strip(), v.strip())
        except Exception:
            pass


_load_dotenv()

from backend import bot, config, db, persistence  # noqa: E402
from backend.schemas import BotConfigRequest, ResetRequest, TradeRequest  # noqa: E402

app = FastAPI(title="Polymarket Paper Trading Bot")

# CORS pour le développement local.
# allow_origins=["*"] est incompatible avec allow_credentials=True (spec CORS) :
# on désactive les credentials, non nécessaires ici (pas de cookies/auth).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instance du bot
bot_instance = bot.TradingBot()


async def _snapshot_loop():
    """Envoie périodiquement un snapshot de la base vers Supabase Storage."""
    while True:
        await asyncio.sleep(config.SNAPSHOT_INTERVAL_SEC)
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, persistence.snapshot_and_upload, db.DB_FILE)
        except Exception:
            pass


@app.on_event("startup")
async def startup_event():
    # Restaure la base depuis Supabase Storage si configuré (AVANT init_db)
    persistence.restore_db(db.DB_FILE)
    db.init_db()
    bot_instance.start()
    if persistence.enabled():
        asyncio.create_task(_snapshot_loop())


@app.on_event("shutdown")
async def shutdown_event():
    bot_instance.stop()
    if persistence.enabled():
        persistence.snapshot_and_upload(db.DB_FILE)


# ============================================================
# PORTEFEUILLE
# ============================================================
@app.get("/api/portfolio")
def get_portfolio():
    try:
        portfolio = db.get_portfolio()
        positions = db.get_positions()

        positions_value = sum(pos["shares"] * pos["current_price"] for pos in positions)
        total_valuation = portfolio["balance"] + positions_value

        guaranteed_payout = sum(pos["shares"] * pos.get("payout_multiplier", 0.0) for pos in positions)
        guaranteed_cost = sum(pos["shares"] * pos["avg_price"] for pos in positions if pos.get("payout_multiplier", 0.0) > 0.0)
        locked_profit = guaranteed_payout - guaranteed_cost

        initial_budget = portfolio["initial_budget"]
        total_return = total_valuation - initial_budget
        roi = (total_return / initial_budget) * 100 if initial_budget > 0 else 0.0

        trades = db.get_trades()
        settled_trades = [t for t in trades if t["pnl"] is not None]
        winning_trades = [t for t in settled_trades if t["pnl"] > 0]
        win_rate = (len(winning_trades) / len(settled_trades)) * 100 if settled_trades else None

        return {
            "balance": portfolio["balance"],
            "initial_budget": initial_budget,
            "positions_value": positions_value,
            "total_valuation": total_valuation,
            "roi": roi,
            "total_return": total_return,
            "locked_profit": locked_profit,
            "win_rate": win_rate,
            "positions": positions,
            "trade_count": len(trades),
            "settled_trade_count": len(settled_trades),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/portfolio/reset")
def reset_portfolio(req: ResetRequest):
    try:
        if req.budget <= 0:
            raise HTTPException(status_code=400, detail="Budget must be greater than zero.")
        db.reset_portfolio(req.budget)
        bot_instance.price_histories.clear()
        bot_instance.peak_prices.clear()
        bot_instance.log(f"Portfolio reset with budget: {req.budget:.2f} USDC", "INFO")
        return {"status": "success", "message": f"Portfolio reset to {req.budget:.2f} USDC"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# MARCHÉS
# ============================================================
@app.get("/api/markets")
async def get_markets():
    try:
        markets = await bot_instance.fetch_markets()
        return markets
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/markets/{market_id}/book")
async def get_market_book(market_id: str):
    try:
        m_details = await bot_instance.fetch_api_json(f"{config.GAMMA_API}/markets/{market_id}")

        clob_token_ids = json.loads(m_details.get("clobTokenIds", "[]"))
        outcomes = json.loads(m_details.get("outcomes", "[]"))

        if not clob_token_ids or len(clob_token_ids) < 2:
            raise HTTPException(status_code=400, detail="Market does not support CLOB order book.")

        yes_book = await bot_instance.fetch_book(clob_token_ids[0])
        no_book = await bot_instance.fetch_book(clob_token_ids[1])

        return {
            "question": m_details.get("question"),
            "outcomes": outcomes,
            "tokens": clob_token_ids,
            "yes_book": yes_book,
            "no_book": no_book,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/trade")
async def execute_manual_trade(req: TradeRequest):
    try:
        res = await bot_instance.place_manual_trade(
            market_id=req.market_id,
            token_id=req.token_id,
            action=req.action,
            outcome=req.outcome,
            amount_usdc=req.amount_usdc,
        )
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================
# BOT
# ============================================================
@app.get("/api/bot/status")
def get_bot_status():
    return {
        "is_running": bot_instance.is_running,
        "strategy": bot_instance.strategy,
        "tick_interval": bot_instance.tick_interval,
        "max_markets_to_scan": bot_instance.max_markets_to_scan,
    }


@app.post("/api/bot/start")
def start_bot():
    success = bot_instance.start()
    return {"status": "success" if success else "already_running"}


@app.post("/api/bot/stop")
def stop_bot():
    success = bot_instance.stop()
    return {"status": "success" if success else "already_stopped"}


@app.post("/api/bot/configure")
def configure_bot(req: BotConfigRequest):
    if req.strategy not in config.VALID_STRATEGIES:
        raise HTTPException(status_code=400, detail="Invalid strategy.")
    if req.tick_interval < 2:
        raise HTTPException(status_code=400, detail="Tick interval must be at least 2 seconds.")

    bot_instance.strategy = req.strategy
    bot_instance.tick_interval = req.tick_interval
    bot_instance.max_markets_to_scan = req.max_markets_to_scan
    bot_instance.log(
        f"Bot reconfigured: strategy={req.strategy}, interval={req.tick_interval}s, max_scan={req.max_markets_to_scan}",
        "INFO",
    )
    return {"status": "success"}


@app.get("/api/logs")
def get_bot_logs():
    return {"logs": bot_instance.get_logs()}


@app.get("/api/trades")
def get_trades_history():
    return {"trades": db.get_trades()}


@app.get("/api/equity-history")
def get_equity_chart_data():
    return {"history": db.get_equity_history()}


@app.get("/api/crypto/signals")
def get_crypto_signals():
    """Derniers signaux calculés par la stratégie crypto Up/Down (pour le dashboard)."""
    return bot_instance.get_crypto_signals()


# ============================================================
# FICHIERS STATIQUES (frontend)
# ============================================================
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
os.makedirs(frontend_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
def serve_index():
    index_path = os.path.join(frontend_dir, "index.html")
    if not os.path.exists(index_path):
        return {"status": "Frontend not ready yet."}
    return FileResponse(index_path)


# ---- PWA : manifest + service worker (servis depuis la racine pour le scope "/") ----
@app.get("/manifest.webmanifest")
def serve_manifest():
    return FileResponse(
        os.path.join(frontend_dir, "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def serve_service_worker():
    return FileResponse(
        os.path.join(frontend_dir, "sw.js"),
        media_type="application/javascript",
    )


@app.get("/favicon.ico")
def serve_favicon():
    return FileResponse(os.path.join(frontend_dir, "icon-192.png"), media_type="image/png")
