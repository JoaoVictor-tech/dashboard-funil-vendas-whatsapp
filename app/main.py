"""Aplicacao FastAPI: serve o dashboard e roda o coletor em segundo plano.

O coletor pode rodar de duas formas (escolha uma):
- APScheduler dentro deste processo  -> SCHEDULER_ATIVO=true (padrao)
- cron do host chamando o container  -> SCHEDULER_ATIVO=false e
  `docker compose exec api python -m app.cli coletar`

Com MODO_DEMO=true o coletor nunca roda: os dados vem de `python -m app.cli demo`.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import TZ, get_settings
from app.dashboard.routes import exigir_login, router as dashboard_router
from app.db import dispose_engine, get_engine

logger = logging.getLogger(__name__)
settings = get_settings()
logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s")

_coleta_em_andamento = asyncio.Lock()
_ultima_coleta: dict[str, Any] = {}


def ultima_coleta() -> dict[str, Any]:
    return dict(_ultima_coleta, em_andamento=_coleta_em_andamento.locked())


async def rodar_coleta(gatilho: str = "agendado") -> dict[str, Any]:
    """Uma coleta por vez: se a anterior ainda roda, esta e ignorada (sem enfileirar)."""
    global _ultima_coleta
    if _coleta_em_andamento.locked():
        logger.info("Coleta ja em andamento - ignorando gatilho '%s'", gatilho)
        return {"status": "ignorado", "motivo": "coleta em andamento"}
    if settings.modo_demo:
        return {"status": "ignorado", "motivo": "modo demo"}
    async with _coleta_em_andamento:
        from app.pipeline import Coletor

        try:
            resultado = await Coletor().executar(gatilho=gatilho)
        except Exception as exc:  # noqa: BLE001
            resultado = {"status": "falhou", "erro": str(exc)[:500]}
        _ultima_coleta = {"quando": datetime.now(TZ).isoformat(), "gatilho": gatilho, **resultado}
        return resultado


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    get_engine()
    scheduler: AsyncIOScheduler | None = None
    if settings.scheduler_ativo and not settings.modo_demo:
        scheduler = AsyncIOScheduler(timezone=str(TZ))
        scheduler.add_job(
            rodar_coleta, IntervalTrigger(minutes=settings.coleta_intervalo_min), id="coleta",
            max_instances=1, coalesce=True, kwargs={"gatilho": "agendado"},
        )
        scheduler.start()
        # Primeira coleta logo depois de subir (em background, para nao segurar o /health).
        asyncio.get_running_loop().call_later(15, lambda: asyncio.create_task(rodar_coleta("subida")))
        logger.info("Scheduler ativo: coleta a cada %s min", settings.coleta_intervalo_min)
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        await dispose_engine()


app = FastAPI(title=settings.dashboard_titulo, lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory="app/dashboard/static"), name="static")
app.include_router(dashboard_router, prefix="/dashboard")


@app.get("/")
async def raiz() -> RedirectResponse:
    return RedirectResponse("/dashboard/", status_code=302)


@app.get("/health")
async def health() -> JSONResponse:
    from sqlalchemy import text

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        banco = "ok"
    except Exception as exc:  # noqa: BLE001
        banco = f"erro: {exc}"[:200]
    return JSONResponse({"status": "ok" if banco == "ok" else "degradado", "banco": banco, "coleta": ultima_coleta()})


@app.post("/admin/coletar")
async def coletar_agora(_: str = Depends(exigir_login)) -> JSONResponse:
    """Forca uma coleta (sem UI): curl -X POST -u usuario:senha https://<dominio>/admin/coletar"""
    if settings.modo_demo:
        return JSONResponse({"status": "ignorado", "motivo": "modo demo"}, status_code=409)
    if _coleta_em_andamento.locked():
        return JSONResponse({"status": "ignorado", "motivo": "coleta em andamento"}, status_code=409)
    asyncio.create_task(rodar_coleta("manual"))
    return JSONResponse({"status": "iniciada"})
