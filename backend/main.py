import asyncio
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
from backend.schemas import BotConfigRequest, ResetRequest, SellRequest  # noqa: E402

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

        # Agrégats vie entière (la fenêtre des 100 derniers trades mentirait)
        tot = db.get_trade_totals()
        win_rate = (tot["wins"] / tot["closed"]) * 100 if tot["closed"] else None

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
            "trade_count": tot["total_trades"],
            "settled_trade_count": tot["closed"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/portfolio/reset")
def reset_portfolio(req: ResetRequest):
    try:
        if req.budget <= 0:
            raise HTTPException(status_code=400, detail="Budget must be greater than zero.")
        db.reset_portfolio(req.budget)
        bot_instance.log(f"Portfolio reset with budget: {req.budget:.2f} USDC", "INFO")
        return {"status": "success", "message": f"Portfolio reset to {req.budget:.2f} USDC"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/positions/purge-old")
async def purge_old_positions():
    """Action unique : ferme tous les paris de l'ANCIEN modèle (vente au bid,
    sinon perte actée) pour repartir sur une base 100 % modèle V2."""
    try:
        res = await bot_instance.purge_old_positions(config.MODEL_V2_CUTOFF)
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/positions/sell")
async def sell_position(req: SellRequest):
    """Vend toute la position (bouton « Vendre tout » du dashboard)."""
    try:
        res = await bot_instance.sell_position(req.token_id)
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
    if req.tick_interval < config.MIN_TICK_INTERVAL:
        raise HTTPException(
            status_code=400,
            detail=f"Tick interval must be at least {config.MIN_TICK_INTERVAL} seconds.",
        )
    bot_instance.tick_interval = req.tick_interval
    bot_instance.log(f"Bot reconfigured: interval={req.tick_interval}s", "INFO")
    return {"status": "success"}


@app.get("/api/logs")
def get_bot_logs():
    return {"logs": bot_instance.get_logs()}


@app.get("/api/trades")
def get_trades_history():
    return {"trades": db.get_trades(), "totals": db.get_trade_totals()}


@app.get("/api/equity-history")
def get_equity_chart_data():
    return {"history": db.get_equity_history()}


@app.get("/api/signals")
def get_signals():
    """Derniers signaux calculés par la stratégie météo (pour le dashboard)."""
    return bot_instance.get_signals()


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
