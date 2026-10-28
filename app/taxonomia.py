"""Vocabulario fechado do monitor: midias, situacoes da conversa e vendedores.

As mesmas chaves alimentam o coletor (classificacao) e o dashboard (rotulos e cores) -
mudar aqui muda nos dois lugares.
"""

from __future__ import annotations

# --- Midias --------------------------------------------------------------------------
# Derivada do utm.source que a Helena grava no clique do anuncio. Cliques do Google nao
# trazem UTM para a Helena, por isso caem em "Sem UTM" junto com organico e direto.
MIDIA_FACEBOOK = "Facebook"
MIDIA_INSTAGRAM = "Instagram"
MIDIA_GOOGLE = "Google"
MIDIA_META_SEM_ID = "Meta — anúncio sem identificação"
MIDIA_SEM_UTM = "Sem UTM (orgânico, Google ou direto)"
MIDIA_OUTROS = "Outros"

MIDIAS_META = (MIDIA_FACEBOOK, MIDIA_INSTAGRAM, MIDIA_META_SEM_ID)
MIDIAS_PAGAS = MIDIAS_META + (MIDIA_GOOGLE,)

# Origens de venda (tabela do DMS cruzada com a Helena) que nao sao midia paga.
ORIGEM_DISPARO = "Disparo WhatsApp (base própria)"
ORIGEM_SEM_CAMPANHA = "Sem origem de campanha"

MIDIA_ORDEM = [
    MIDIA_FACEBOOK, MIDIA_INSTAGRAM, MIDIA_GOOGLE, MIDIA_META_SEM_ID, MIDIA_SEM_UTM,
    MIDIA_OUTROS, ORIGEM_DISPARO, ORIGEM_SEM_CAMPANHA,
]
MIDIA_COR = {
    MIDIA_FACEBOOK: "#2563eb", MIDIA_INSTAGRAM: "#db2777", MIDIA_GOOGLE: "#d97706",
    MIDIA_META_SEM_ID: "#475569", MIDIA_SEM_UTM: "#0d9488", MIDIA_OUTROS: "#94a3b8",
    ORIGEM_DISPARO: "#64748b", ORIGEM_SEM_CAMPANHA: "#cbd5e1",
}

CAMPANHA_NAO_IDENTIFICADA = "Anúncio Meta não identificado"
CAMPANHA_GOOGLE = "Google Ads"


def midia_de(utm_source: str | None) -> str:
    if not utm_source:
        return MIDIA_SEM_UTM
    s = utm_source.strip().upper()
    if s == "FACEBOOK":
        return MIDIA_FACEBOOK
    if s == "INSTAGRAM":
        return MIDIA_INSTAGRAM
    if s == "GOOGLE":
        return MIDIA_GOOGLE
    if s.startswith("AD -") or s.startswith("AD-") or s.startswith("MESSENGER") or s.startswith("- ("):
        return MIDIA_META_SEM_ID
    return MIDIA_OUTROS


# --- Google Ads: quais acoes de conversao contam como "lead" ------------------------
# O total de "conversoes" do Google mistura ligacao, rota, visita a loja etc. Para CPL,
# so contam as acoes de contato. Cada conta nomeia as acoes do seu jeito ("Contato - botão
# Whatsapp", "Lead form - Submit", "Site - 0km click_whatsapp"...), por isso o casamento e
# por palavra.
ACOES_GOOGLE_LEAD_PALAVRAS = ("whatsapp", "conversation started", "lead form")


def acao_google_e_lead(nome: str) -> bool:
    n = nome.lower()
    return any(p in n for p in ACOES_GOOGLE_LEAD_PALAVRAS)

# --- Situacao da conversa (Tela 2) ------------------------------------------------------
SITUACOES = {
    "comprou": "Comprou",
    "perdido": "Perdido (classificação)",
    "encerrada": "Encerrada sem classificação",
    "andamento": "Em andamento",
}
SITUACAO_COR = {"comprou": "#15803d", "perdido": "#b91c1c", "encerrada": "#94a3b8", "andamento": "#2563eb"}

# --- Vendedores: nome na Helena -> login no DMS -------------------------------------
# A Helena guarda o nome do agente; o DMS guarda o login do vendedor. E o unico
# cruzamento manual do monitor - ao entrar vendedor novo, acrescentar aqui. O mapa e
# unico porque o mesmo vendedor pode faturar nas duas lojas. Quem so aparece no DMS
# entra com o nome tirado do login.
VENDEDORES: dict[str, str] = {
    # Centro
    "Ana": "ANA.P", "Bruno": "BRUNO.S", "Carla": "CARLA.M", "Diego": "DIEGO.R",
    "Elisa": "ELISA.T", "Fábio": "FABIO.C", "Gabriela": "GABRIELA.L",
    # Zona Norte
    "Heitor": "HEITOR.A", "Isabela": "ISABELA.F", "João P.": "JOAO.P", "Larissa": "LARISSA.N",
    "Marcos": "MARCOS.V", "Natália": "NATALIA.B",
}
CODIGO_PARA_NOME = {codigo: nome for nome, codigo in VENDEDORES.items()}


# Agentes cujo primeiro nome colidiria com outro vendedor (ou cujo login do DMS usa o
# sobrenome): o nome completo na Helena -> nome usado no monitor.
NOMES_COMPOSTOS = {
    "João Pedro Alves": "João P.",
}


def primeiro_nome(nome_agente: str | None) -> str | None:
    if not nome_agente:
        return None
    nome = nome_agente.strip()
    if nome in NOMES_COMPOSTOS:
        return NOMES_COMPOSTOS[nome]
    return nome.split()[0].title()


def nome_vendedor(codigo_dms: str | None) -> str:
    if not codigo_dms:
        return "—"
    return CODIGO_PARA_NOME.get(codigo_dms, codigo_dms.replace(".", " ").title().strip())
