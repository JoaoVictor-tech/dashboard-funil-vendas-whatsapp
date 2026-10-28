"""Dados de demonstracao: uma concessionaria ficticia com duas unidades.

Gera, para os ultimos N dias, o que o coletor e a integracao do DMS gravariam - midia paga
por dia e campanha, anuncios, conversas, disparos de WhatsApp e vendas - com as mesmas
relacoes entre si (o anuncio do clique liga a conversa a campanha; o telefone e o contato
ligam a venda a conversa e ao disparo). Assim todas as consultas do painel tem o que mostrar.

    python -m app.cli demo                 # 120 dias, semente fixa (mesmo resultado sempre)
    python -m app.cli demo --dias 60 --semente 3

So roda com MODO_DEMO=true: apaga e recria o conteudo das tabelas mf_* e vendas_dms.
"""

from __future__ import annotations

import json
import random
import uuid
import zlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import text

from app import repository as repo
from app import taxonomia as tx
from app.config import TZ, get_settings
from app.db import get_engine

TABELAS = ["mf_conversas", "mf_midia_diaria", "mf_midia_diaria_total", "mf_anuncios",
           "mf_templates", "mf_disparos", "mf_monitor_execucoes", "mf_monitor_estado", "vendas_dms"]


@dataclass
class CampanhaMeta:
    nome: str
    verba_dia: float
    cpl: float              # custo por conversa iniciada que a Meta reporta
    parcela_instagram: float
    inicio: int = 0         # dia relativo (0 = primeiro dia gerado) em que a campanha comeca
    fim: int | None = None  # ultimo dia relativo (None = ate hoje)
    engajamento: bool = False


UNIDADES: dict[str, dict[str, Any]] = {
    "centro": {
        "meta": [
            CampanhaMeta("AutoVale_Centro_Seminovos_Leads", 390, 24.0, 0.62),
            CampanhaMeta("AutoVale_Centro_0km_Ofertas", 520, 34.0, 0.55),
            CampanhaMeta("AutoVale_Centro_Feirao_Mensal", 270, 19.0, 0.70, inicio=20),
            CampanhaMeta("AutoVale_Centro_Queima_Estoque", 330, 21.0, 0.58, fim=82),
            CampanhaMeta("AutoVale_Centro_Marca_Engajamento", 90, 0, 0.75, engajamento=True),
        ],
        "google": [("AutoVale_Centro_Pesquisa_Marca", "SEARCH", 180, 15.0), ("AutoVale_Centro_PMax_Seminovos", "PERFORMANCE_MAX", 230, 27.0)],
        "vendedores": ["Ana", "Bruno", "Carla", "Diego", "Elisa", "Fábio", "Gabriela"],
        "vendas_balcao_dia": 1.4,
    },
    "norte": {
        "meta": [
            CampanhaMeta("AutoVale_Norte_Seminovos_Leads", 320, 26.0, 0.66),
            CampanhaMeta("AutoVale_Norte_SUV_Lancamento", 440, 38.0, 0.52, inicio=35),
            CampanhaMeta("AutoVale_Norte_Revisao_Agendada", 140, 16.0, 0.60),
        ],
        "google": [("AutoVale_Norte_Pesquisa_Marca", "SEARCH", 150, 17.0)],
        "vendedores": ["Heitor", "Isabela", "João P.", "Larissa", "Marcos", "Natália"],
        "vendas_balcao_dia": 1.1,
    },
}
MODELOS_DISPARO = {
    "centro": ["feirao_fim_de_semana_centro", "seminovos_condicao_especial_centro", "convite_test_drive_centro"],
    "norte": ["revisao_com_desconto_norte", "lancamento_suv_norte"],
}


class Gerador:
    def __init__(self, dias: int, semente: int) -> None:
        self.r = random.Random(semente)
        self.hoje = datetime.now(TZ).date()
        self.primeiro = self.hoje - timedelta(days=dias - 1)
        self.dias = dias
        self.telefone_seq = 0
        self.midia: list[dict[str, Any]] = []
        self.totais: list[dict[str, Any]] = []
        self.anuncios: list[dict[str, Any]] = []
        self.conversas: list[dict[str, Any]] = []
        self.templates: list[dict[str, Any]] = []
        self.disparos: list[dict[str, Any]] = []
        self.vendas: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- utilitarios
    def _telefone(self) -> str:
        self.telefone_seq += 1
        return f"1190{self.telefone_seq:07d}"   # faixa ficticia, sequencial

    def _momento(self, dia: date) -> datetime:
        """Horario comercial com pico no fim da tarde."""
        hora = min(21, max(8, int(self.r.gauss(15, 3))))
        return datetime.combine(dia, time(hora, self.r.randint(0, 59), self.r.randint(0, 59)), tzinfo=TZ)

    def _poisson(self, media: float) -> int:
        # soma de Bernoullis: suficiente para medias pequenas e sem depender de numpy
        n = int(media * 3) + 3
        return sum(1 for _ in range(n) if self.r.random() < media / n)

    def _fator_dia(self, dia: date) -> float:
        return (0.75 if dia.weekday() == 6 else 1.15 if dia.weekday() == 5 else 1.0) * self.r.uniform(0.8, 1.2)

    # ---------------------------------------------------------------- midia
    def gerar(self) -> None:
        for loja, cfg in UNIDADES.items():
            ads = {c.nome: [str(self.r.randint(10**17, 10**18 - 1)) for _ in range(3)] for c in cfg["meta"]}
            for c in cfg["meta"]:
                for i, ad in enumerate(ads[c.nome], 1):
                    self.anuncios.append({"loja": loja, "anuncio_id": ad, "anuncio_nome": f"{c.nome.split('_', 2)[-1]} - criativo {i}",
                                          "campanha": c.nome, "dia": self.primeiro + timedelta(days=c.inicio)})
            for n in range(self.dias):
                dia = self.primeiro + timedelta(days=n)
                self._dia(loja, cfg, ads, n, dia)
            self._disparos(loja)
            self._vendas_balcao(loja, cfg)

    def _dia(self, loja: str, cfg: dict[str, Any], ads: dict[str, list[str]], n: int, dia: date) -> None:
        f = self._fator_dia(dia)
        tot = {"invest_meta": 0.0, "invest_facebook": 0.0, "invest_instagram": 0.0,
               "conversas_meta": 0, "conversas_facebook": 0, "conversas_instagram": 0}
        for c in cfg["meta"]:
            if n < c.inicio or (c.fim is not None and n > c.fim):
                continue
            inv = round(c.verba_dia * f, 2)
            ig = round(inv * c.parcela_instagram, 2)
            outros = round(inv * 0.03, 2)
            fb = round(inv - ig - outros, 2)
            res = 0 if c.engajamento else max(0, round(inv / (c.cpl * self.r.uniform(0.85, 1.15))))
            res_ig = round(res * c.parcela_instagram * self.r.uniform(0.95, 1.08))
            res_ig = min(res, res_ig)
            self.midia.append({"loja": loja, "dia": dia, "plataforma": "meta", "campanha": c.nome, "campanha_id": None,
                               "tipo": "Post engagement" if c.engajamento else "Messaging conversations started",
                               "investimento": inv, "resultados": res, "alcance": int(inv * 38), "impressoes": int(inv * 95),
                               "cliques": int(inv * 1.6), "invest_facebook": fb, "invest_instagram": ig, "invest_outros": outros,
                               "resultados_facebook": res - res_ig, "resultados_instagram": res_ig})
            tot["invest_meta"] += inv; tot["invest_facebook"] += fb; tot["invest_instagram"] += ig
            tot["conversas_meta"] += res; tot["conversas_facebook"] += res - res_ig; tot["conversas_instagram"] += res_ig
            if c.engajamento:
                # campanha de engajamento: a conta registra algumas conversas que a tabela por
                # campanha nao mostra (a tela avisa que a soma fica abaixo do total)
                extra = self._poisson(1.5)
                tot["conversas_meta"] += extra; tot["conversas_instagram"] += extra
                continue
            # Nem todo clique vira conversa com UTM: ~78% chegam, o resto se perde no caminho.
            for _ in range(self._poisson(res * 0.78)):
                ig_clique = self.r.random() < c.parcela_instagram
                sorte = self.r.random()
                if sorte < 0.05:      # anuncio que a Meta manda sem identificacao
                    fonte, anuncio = "AD - " + str(self.r.randint(1000, 9999)), None
                elif sorte < 0.09:    # anuncio de conta/campanha fora do mapa anuncio -> campanha
                    fonte, anuncio = "INSTAGRAM" if ig_clique else "FACEBOOK", str(self.r.randint(10**17, 10**18 - 1))
                else:
                    fonte, anuncio = "INSTAGRAM" if ig_clique else "FACEBOOK", self.r.choice(ads[c.nome])
                self._conversa(loja, cfg, dia, fonte, anuncio, taxa_compra=0.02)

        acoes: dict[str, float] = {}
        inv_g = conv_g = 0.0
        for nome, tipo, verba, cpl in cfg["google"]:
            inv = round(verba * f, 2)
            conv = round(inv / (cpl * self.r.uniform(0.8, 1.2)), 1)
            cliques = int(inv * 0.9)
            self.midia.append({"loja": loja, "dia": dia, "plataforma": "google", "campanha": nome, "campanha_id": str(zlib.crc32(nome.encode())),
                               "tipo": tipo, "investimento": inv, "resultados": conv, "alcance": None, "impressoes": cliques * 14,
                               "cliques": cliques, "invest_facebook": 0, "invest_instagram": 0, "invest_outros": 0,
                               "resultados_facebook": 0, "resultados_instagram": 0})
            inv_g += inv; conv_g += conv
        whats = round(conv_g * 0.55, 1); form = round(conv_g * 0.12, 1)
        acoes = {"Contato - botão WhatsApp": whats, "Lead form - Submit": form,
                 "Ligação do anúncio": round(conv_g * 0.18, 1), "Rotas para a loja": round(conv_g - whats - form - round(conv_g * 0.18, 1), 1)}
        leads_g = sum(v for k, v in acoes.items() if tx.acao_google_e_lead(k))
        self.totais.append({"loja": loja, "dia": dia, **{k: round(v, 2) if isinstance(v, float) else v for k, v in tot.items()},
                            "invest_google": round(inv_g, 2), "conversoes_google": round(conv_g, 2), "leads_google": round(leads_g, 2),
                            "acoes_google": json.dumps(acoes, ensure_ascii=False)})
        # Google: cliques no WhatsApp chegam quase sempre sem UTM; alguns (site com UTM) chegam com GOOGLE.
        for _ in range(self._poisson(leads_g * 0.7)):
            self._conversa(loja, cfg, dia, "GOOGLE" if self.r.random() < 0.3 else None, None, taxa_compra=0.022)
        for _ in range(self._poisson(4)):   # organico e contato direto
            self._conversa(loja, cfg, dia, None, None, taxa_compra=0.04)

    # ---------------------------------------------------------------- conversas
    def _conversa(self, loja: str, cfg: dict[str, Any], dia: date, fonte: str | None, anuncio: str | None,
                  taxa_compra: float, origem: str = "Contato", criado: datetime | None = None,
                  contato: str | None = None, telefone: str | None = None, departamento: str | None = None) -> dict[str, Any]:
        criado = criado or self._momento(dia)
        idade = (self.hoje - criado.date()).days
        vendedor = self.r.choice(cfg["vendedores"])
        atendida = self.r.random() < 0.9
        encerrada = idade > 3 and self.r.random() < 0.8
        perdida = encerrada and self.r.random() < 0.3
        c = {
            "session_id": str(uuid.UUID(int=self.r.getrandbits(128))), "loja": loja, "numero": None,
            "criado_em": criado, "atualizado_em_origem": criado + timedelta(hours=self.r.randint(1, 72)),
            "status": "COMPLETED" if encerrada else "IN_PROGRESS", "origem": origem, "canal_id": None,
            "departamento": departamento or "Vendas", "user_id": None, "vendedor": vendedor,
            "contato_id": contato or str(uuid.UUID(int=self.r.getrandbits(128))), "telefone": telefone or self._telefone(),
            "utm_source": fonte, "utm_campaign": anuncio, "utm_headline": None, "midia": tx.midia_de(fonte), "campanha": None,
            "primeira_resposta_em": criado + timedelta(minutes=self.r.randint(1, 90)) if atendida else None,
            "ultima_msg_cliente": criado, "ultima_msg_equipe": criado + timedelta(minutes=95) if atendida else None,
            "encerrada_em": criado + timedelta(days=self.r.randint(1, 3)) if encerrada else None,
            "classificacao": "LOST" if perdida else None, "bot_id": None, "disparos_verificados_em": None,
        }
        c["disparos_verificados_em"] = c["atualizado_em_origem"]   # varredura de disparos em dia
        self.conversas.append(c)
        if origem == "Contato" and not perdida and self.r.random() < taxa_compra:
            quando = criado + timedelta(days=self.r.randint(2, 25), hours=self.r.randint(0, 6))
            if quando.date() <= self.hoje:
                paga = (fonte or "").upper() in ("FACEBOOK", "INSTAGRAM", "GOOGLE") or (fonte or "").startswith("AD -")
                self._venda(loja, quando, tx.VENDEDORES[vendedor], c["telefone"], c["contato_id"],
                            veio_campanha=paga, utm_source=fonte if paga else None, utm_campaign=anuncio if paga else None)
        return c

    def _disparos(self, loja: str) -> None:
        cfg = UNIDADES[loja]
        for nome in MODELOS_DISPARO[loja]:
            tid = str(uuid.UUID(int=self.r.getrandbits(128)))
            self.templates.append({"id": tid, "loja": loja, "nome": nome, "canal_id": None, "tipo": "MARKETING"})
            # cada modelo e usado em 2 ou 3 datas espalhadas pelo periodo
            for _ in range(self.r.randint(2, 3)):
                dia = self.primeiro + timedelta(days=self.r.randint(0, self.dias - 2))
                envio = datetime.combine(dia, time(10, self.r.randint(0, 59)), tzinfo=TZ)
                for _ in range(self.r.randint(120, 380)):
                    respondeu = self.r.random() < 0.16
                    conversa = self._conversa(loja, cfg, dia, None, None, taxa_compra=0, origem="Campanha",
                                              criado=envio, departamento=f"Disparos {loja.title()}")
                    resposta = envio + timedelta(minutes=self.r.randint(5, 600)) if respondeu else None
                    self.disparos.append({"mensagem_id": str(uuid.UUID(int=self.r.getrandbits(128))), "loja": loja,
                                          "session_id": conversa["session_id"], "contato_id": conversa["contato_id"],
                                          "telefone": conversa["telefone"], "template_id": tid,
                                          "enviado_em": envio, "respondeu_em": resposta})
                    if respondeu and self.r.random() < 0.09:
                        quando = envio + timedelta(days=self.r.randint(1, 20))
                        if quando.date() <= self.hoje:
                            self._venda(loja, quando, tx.VENDEDORES[conversa["vendedor"]], conversa["telefone"],
                                        conversa["contato_id"], veio_campanha=True, utm_source=None, utm_campaign=None)

    # ---------------------------------------------------------------- vendas
    def _venda(self, loja: str, quando: datetime, vendedor: str, telefone: str | None, contato: str | None,
               veio_campanha: bool, utm_source: str | None, utm_campaign: str | None) -> None:
        self.vendas.append({"loja": loja, "data_venda": quando, "vendedor": vendedor,
                            "valor_vendido": round(self.r.uniform(72_000, 260_000), -2), "telefone_normalizado": telefone,
                            "helena_contact_id": contato, "veio_campanha": veio_campanha,
                            "utm_source": utm_source, "utm_campaign": utm_campaign})

    def _vendas_balcao(self, loja: str, cfg: dict[str, Any]) -> None:
        """Vendas sem origem digital: cliente que entrou na loja, indicacao, telefone."""
        for n in range(self.dias):
            dia = self.primeiro + timedelta(days=n)
            for _ in range(self._poisson(cfg["vendas_balcao_dia"])):
                vendedor = tx.VENDEDORES[self.r.choice(cfg["vendedores"])]
                self._venda(loja, self._momento(dia), vendedor, self._telefone(), None, False, None, None)


async def popular(dias: int = 120, semente: int = 42) -> dict[str, int]:
    if not get_settings().modo_demo:
        raise RuntimeError("Recusado: `demo` apaga as tabelas do monitor. Rode so com MODO_DEMO=true.")
    g = Gerador(dias, semente)
    g.gerar()
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABELAS)} RESTART IDENTITY"))
        await _inserir(conn, "mf_midia_diaria", g.midia)
        await _inserir(conn, "mf_midia_diaria_total", g.totais, conversoes={"acoes_google": "jsonb"})
        await _inserir(conn, "mf_anuncios", [{"anuncio_id": a["anuncio_id"], "loja": a["loja"], "anuncio_nome": a["anuncio_nome"],
                                               "campanha": a["campanha"], "primeiro_dia": a["dia"], "ultimo_dia": g.hoje} for a in g.anuncios])
        await _inserir(conn, "mf_conversas", g.conversas)
        await _inserir(conn, "mf_templates", g.templates)
        await _inserir(conn, "mf_disparos", g.disparos)
        await _inserir(conn, "vendas_dms", g.vendas)
        # mesmo passo do coletor: liga cada conversa a campanha pelo ID do anuncio
        await repo.resolver_campanhas(conn)
    return {"dias": dias, "conversas": len(g.conversas), "disparos": len(g.disparos), "vendas": len(g.vendas),
            "linhas_midia": len(g.midia), "anuncios": len(g.anuncios)}


async def _inserir(conn: Any, tabela: str, linhas: list[dict[str, Any]], conversoes: dict[str, str] | None = None) -> None:
    if not linhas:
        return
    colunas = list(linhas[0])
    valores = ", ".join(f"CAST(:{c} AS {conversoes[c]})" if conversoes and c in conversoes else f":{c}" for c in colunas)
    await conn.execute(text(f"INSERT INTO {tabela} ({', '.join(colunas)}) VALUES ({valores})"), linhas)
