"""Linha de comando do monitor.

    python -m app.cli coletar                 # coleta incremental (conversas + ultimos 7 dias de midia)
    python -m app.cli coletar --dias-midia 120  # rebaixa N dias de midia (backfill)
    python -m app.cli testar-conexoes         # Postgres, Helena, Reportei
    python -m app.cli mapear-anuncios --de 2025-10-01 --ate 2026-05-31
                                              # so o mapa anuncio->campanha de meses antigos (uma chamada por mes),
                                              # para que vendas de leads antigos apontem a campanha certa
    python -m app.cli demo --dias 120         # MODO_DEMO=true: popula o banco com dados ficticios
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta

from app.config import get_settings
from app.db import dispose_engine


async def _coletar(dias_midia: int | None, conversas_desde: date | None, disparos_limite: int) -> None:
    from app.pipeline import Coletor

    resultado = await Coletor().executar(gatilho="cli", dias_midia=dias_midia, conversas_desde=conversas_desde,
                                         disparos_limite=disparos_limite)
    print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))


async def _testar() -> None:
    from sqlalchemy import text

    from app.clients.helena import HelenaClient
    from app.clients.reportei import ReporteiClient
    from app.db import get_engine

    async with get_engine().connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM vendas_dms"))).scalar()
        print(f"Postgres: ok ({n} vendas no DMS)")
    s = get_settings()
    for u in s.unidades:
        if not u.tem_conversas:
            print(f"Helena [{u.nome}]: sem token - conversas nao conectadas")
            continue
        helena = HelenaClient(u.helena_token)
        try:
            contador = 0
            async for _ in helena.sessoes(criadas_apos=datetime.now().astimezone() - timedelta(days=1)):
                contador += 1
                if contador >= 100:
                    break
            print(f"Helena [{u.nome}]: ok ({contador}+ conversas nas ultimas 24h)")
        finally:
            await helena.fechar()
    reportei = ReporteiClient()
    try:
        ontem = date.today() - timedelta(days=1)
        for u in s.unidades:
            meta = (await reportei.meta_totais(u.reportei_meta_id, ontem, ontem))["fb_ads:spend"] if u.tem_meta else None
            google = (await reportei.google(u.reportei_google_id, ontem, ontem))["custo"] if u.tem_google else None
            print(f"Reportei [{u.nome}]: ok (ontem Meta {'R$ %.2f' % meta if meta is not None else 'sem integracao'}, "
                  f"Google {'R$ %.2f' % google if google is not None else 'sem integracao'})")
    finally:
        await reportei.fechar()


async def _mapear_anuncios(de: date, ate: date) -> None:
    from app import repository as repo
    from app.clients.reportei import ReporteiClient
    from app.db import get_engine

    reportei = ReporteiClient()
    total = 0
    try:
        for u in get_settings().unidades:
            if not u.tem_meta:
                continue
            inicio = de
            while inicio <= ate:
                fim = min((inicio.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1), ate)
                anuncios = await reportei.meta_anuncios(u.reportei_meta_id, inicio, fim)
                async with get_engine().begin() as conn:
                    total += await repo.upsert_anuncios(conn, u.chave, inicio, anuncios)
                    await repo.resolver_campanhas(conn)
                print(f"[{u.nome}] {inicio:%Y-%m}: {len(anuncios)} anuncios")
                inicio = fim + timedelta(days=1)
    finally:
        await reportei.fechar()
    print(f"{total} anuncios gravados/atualizados")


async def _demo(dias: int, semente: int) -> None:
    from app.demo import popular

    print(json.dumps(await popular(dias, semente), ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s")
    parser = argparse.ArgumentParser(prog="monitor-funil")
    sub = parser.add_subparsers(dest="comando", required=True)
    c = sub.add_parser("coletar")
    c.add_argument("--dias-midia", type=int, default=None, help="quantos dias de midia rebaixar (padrao: 7)")
    c.add_argument("--conversas-desde", type=date.fromisoformat, default=None, help="recarrega TODAS as conversas criadas a partir desta data")
    c.add_argument("--disparos", type=int, default=400, help="quantos disparos identificar (template) neste ciclo")
    sub.add_parser("testar-conexoes")
    ma = sub.add_parser("mapear-anuncios")
    ma.add_argument("--de", type=date.fromisoformat, required=True)
    ma.add_argument("--ate", type=date.fromisoformat, required=True)
    d = sub.add_parser("demo")
    d.add_argument("--dias", type=int, default=120)
    d.add_argument("--semente", type=int, default=42)
    args = parser.parse_args(argv)

    async def _run() -> None:
        try:
            if args.comando == "coletar":
                await _coletar(args.dias_midia, args.conversas_desde, args.disparos)
            elif args.comando == "demo":
                await _demo(args.dias, args.semente)
            elif args.comando == "mapear-anuncios":
                await _mapear_anuncios(args.de, args.ate)
            else:
                await _testar()
        finally:
            await dispose_engine()

    asyncio.run(_run())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERRO: {exc}", file=sys.stderr)
        raise SystemExit(1)
