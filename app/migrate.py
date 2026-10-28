"""Aplicador de migracoes simples: roda os arquivos de sql/ em ordem, uma unica vez cada.

Escolhido em vez de Alembic porque o banco pode ser compartilhado com outras aplicacoes -
assim o mesmo SQL pode ser colado direto num console do Postgres, sem depender do Python.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys

from sqlalchemy import text

from app.db import dispose_engine, get_engine

logger = logging.getLogger(__name__)
SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"


async def aplicar() -> list[str]:
    engine = get_engine()
    aplicadas: list[str] = []
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS mf_schema_migrations (
                    versao      text PRIMARY KEY,
                    aplicado_em timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        )
        rows = await conn.execute(text("SELECT versao FROM mf_schema_migrations"))
        ja_aplicadas = {r[0] for r in rows}

    for arquivo in sorted(SQL_DIR.glob("*.sql")):
        versao = arquivo.name
        if versao in ja_aplicadas:
            continue
        sql = arquivo.read_text(encoding="utf-8")
        logger.info("Aplicando migracao %s", versao)
        async with engine.begin() as conn:
            # ATENCAO: nao dá para usar conn.execute(text(sql)) aqui. O asyncpg manda todo
            # comando pelo protocolo de prepared statement, que aceita UM comando por vez -
            # um arquivo .sql com varios CREATE/GRANT estoura com "cannot insert multiple
            # commands into a prepared statement". A conexao crua do asyncpg usa o protocolo
            # simples, que aceita o script inteiro, e continua dentro desta transacao (ou
            # seja: se qualquer comando falhar, a migracao inteira volta atras).
            bruta = await conn.get_raw_connection()
            await bruta.driver_connection.execute(sql)
            await conn.execute(
                text("INSERT INTO mf_schema_migrations (versao) VALUES (:v)"), {"v": versao}
            )
        aplicadas.append(versao)

    return aplicadas


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
    try:
        aplicadas = await aplicar()
    finally:
        await dispose_engine()
    if aplicadas:
        print("Migracoes aplicadas: " + ", ".join(aplicadas))
    else:
        print("Banco ja estava atualizado.")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception as exc:  # pragma: no cover
        print(f"ERRO na migracao: {exc}", file=sys.stderr)
        raise SystemExit(1)
