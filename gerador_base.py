# -*- coding: utf-8 -*-
"""
Gerador de base sintética de vendas — distribuição alimentar B2B.

Produz uma base de vendas mensais no grão (PDV, mês, categoria) com as
propriedades estatísticas de uma carteira real de distribuidor, e com casos
de queda/churn PLANTADOS de propósito, para que a metodologia de detecção
(ver `termometro.py`) tenha o que encontrar.

Nada aqui vem de base real: nomes de PDV, cidades e categorias são inventados
por composição. O que é reaproveitado é só a *forma* dos dados.

Propriedades calibradas
-----------------------
Concentração (Pareto)   top 1% ~25% da receita, top 10% ~65%, top 20% ~80%
Sazonalidade            vale jan/fev ~80% da média, pico ago-dez ~110%
Crescimento agregado    ~12% ao ano
Regimes de compra       ~50% mensal, ~25% cíclico (2-3 meses), ~25% esporádico
Grão                    uma linha por (PDV, mês, categoria); sem nº de pedido

Nota sobre a concentração: para uma lognormal, a fração da receita detida pelo
topo q é  1 - Phi(Phi^-1(1-q) - sigma).  Resolvendo para o alvo do top 10% = 65%
sai sigma ~ 1.667 — e esse mesmo sigma entrega top 1% = 25.5% e top 20% = 79.5%.
Os três alvos do briefing são consistentes entre si e caem de um único parâmetro.
Aqui o sigma da lognormal é menor porque o multiplicador de tipologia adiciona
variância; a soma das duas é que precisa chegar perto de 1.667.

Nota sobre o horizonte: o dashboard analisa 24 meses. A base tem 60 porque o
backtest de calibração precisa cortar no passado e olhar 12 meses à frente —
com 24 meses não sobra horizonte fora da amostra.

Saídas (em ./dados)
-------------------
vendas.csv.gz       fato: pdv_id, mes, categoria_id, valor
dim_pdv.csv         dimensão de PDV + vendedor/supervisor + gabarito do caso plantado
dim_categoria.csv   dimensão de categoria (familia, linha)
dim_mes.csv         dimensão de calendário
amostra_vendas.csv  primeiras 3.000 linhas do fato, legível sem descompactar
resumo_geracao.json métricas de validação da própria geração

Uso:  python gerador_base.py
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import random
from collections import defaultdict

# ----------------------------------------------------------------------------
# Parâmetros
# ----------------------------------------------------------------------------

SEED = 20260808

ANO_INI, MES_INI = 2021, 8          # primeiro mês da base
N_MESES = 60                        # até 2026-07 (último mês fechado)

N_PDV = 4200                        # cadastrados ao longo dos 5 anos
N_VENDEDORES = 90
N_SUPERVISORES = 7
CARTEIRA_MIN, CARTEIRA_MAX = 30, 170

SIGMA_LOG = 1.200                    # dispersão do porte (ver nota sobre Pareto)
MEDIANA_BASE_MENSAL = 1900.0        # R$/mês do PDV mediano, antes de tipologia
CRESCIMENTO_ANUAL = 1.046           # calibrado p/ ~12% medido no agregado
FRACAO_LEGADO = 0.50                # PDVs que já estavam na carteira no mês 0
RUIDO_SIGMA = 0.30                  # ruído multiplicativo mês a mês
DRIFT_SIGMA = 0.17                  # tendência própria de cada PDV, média zero

PROB_LINHA_ZERO = 0.055             # linha de R$ 0,00 dentro de um mês com compra
PROB_MES_FANTASMA = 0.020           # mês só com linhas de R$ 0,00 (a pegadinha)

FRACAO_ATRITO = 0.30                # PDVs que somem por atrito natural

DIR_SAIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados")

# ----------------------------------------------------------------------------
# Calendário
# ----------------------------------------------------------------------------


def ym(t: int) -> tuple[int, int]:
    """Índice de mês (0 = ANO_INI/MES_INI) -> (ano, mês)."""
    m = MES_INI - 1 + t
    return ANO_INI + m // 12, m % 12 + 1


def rotulo(t: int) -> str:
    a, m = ym(t)
    return f"{a}-{m:02d}"


def mes_civil(t: int) -> int:
    return ym(t)[1]


# Índice sazonal padrão: vale em jan/fev, pico ago-dez. Média = 1,0.
SAZONAL_PADRAO = [0.78, 0.82, 0.95, 0.97, 1.00, 0.98, 1.02, 1.08, 1.08, 1.10, 1.14, 1.12]

# Sorveteria/açaiteria puxam pelo calor: perfil invertido.
SAZONAL_VERAO = [1.20, 1.15, 1.05, 0.95, 0.85, 0.78, 0.80, 0.85, 0.95, 1.05, 1.15, 1.22]

# Buffet e eventos: morre em janeiro, explode em novembro/dezembro.
SAZONAL_EVENTOS = [0.55, 0.70, 0.90, 0.95, 1.05, 1.05, 0.95, 1.00, 1.05, 1.15, 1.35, 1.30]


def _normaliza(v: list[float]) -> list[float]:
    m = sum(v) / len(v)
    return [x / m for x in v]


SAZONAL_PADRAO = _normaliza(SAZONAL_PADRAO)
SAZONAL_VERAO = _normaliza(SAZONAL_VERAO)
SAZONAL_EVENTOS = _normaliza(SAZONAL_EVENTOS)

# ----------------------------------------------------------------------------
# Dimensões inventadas
# ----------------------------------------------------------------------------

# (nome, peso na base, multiplicador de porte, perfil sazonal, famílias preferidas)
TIPOLOGIAS = [
    ("Padaria",            18.0, 0.80, "padrao",   ["Farinhas e Misturas", "Manteigas e Margarinas", "Recheios e Confeitaria", "Leites e Cremes", "Açúcares e Adoçantes", "Embalagens"]),
    ("Minimercado",        16.0, 0.70, "padrao",   ["Bebidas Frias", "Enlatados", "Grãos e Cereais", "Higiene e Limpeza", "Frios e Embutidos", "Massas Secas"]),
    ("Restaurante",        14.0, 0.95, "padrao",   ["Proteínas Congeladas", "Óleos e Gorduras", "Molhos e Condimentos", "Grãos e Cereais", "Conservas", "Descartáveis"]),
    ("Lanchonete",         11.0, 0.60, "padrao",   ["Frios e Embutidos", "Congelados Salgados", "Molhos e Condimentos", "Bebidas Frias", "Descartáveis", "Embalagens"]),
    ("Pizzaria",            8.0, 0.78, "padrao",   ["Queijos", "Massas Frescas", "Molhos e Condimentos", "Frios e Embutidos", "Conservas", "Embalagens"]),
    ("Supermercado",        7.0, 3.60, "padrao",   ["Bebidas Frias", "Higiene e Limpeza", "Enlatados", "Grãos e Cereais", "Frios e Embutidos", "Congelados Doces", "Sorvetes e Gelados", "Massas Secas"]),
    ("Cafeteria",           6.0, 0.52, "padrao",   ["Bebidas Quentes", "Leites e Cremes", "Chocolates e Coberturas", "Recheios e Confeitaria", "Descartáveis", "Açúcares e Adoçantes"]),
    ("Hamburgueria",        5.0, 0.72, "padrao",   ["Proteínas Congeladas", "Queijos", "Molhos e Condimentos", "Congelados Salgados", "Embalagens", "Descartáveis"]),
    ("Sorveteria",          4.0, 0.58, "verao",    ["Sorvetes e Gelados", "Leites e Cremes", "Chocolates e Coberturas", "Sucos e Polpas", "Descartáveis", "Congelados Doces"]),
    ("Açaiteria",           3.0, 0.50, "verao",    ["Sucos e Polpas", "Sorvetes e Gelados", "Chocolates e Coberturas", "Grãos e Cereais", "Descartáveis", "Embalagens"]),
    ("Loja de Conveniência", 3.0, 0.46, "padrao",  ["Bebidas Frias", "Chocolates e Coberturas", "Frios e Embutidos", "Higiene e Limpeza", "Congelados Salgados", "Bebidas Quentes"]),
    ("Buffet e Eventos",    2.5, 1.15, "eventos",  ["Proteínas Congeladas", "Congelados Salgados", "Descartáveis", "Bebidas Frias", "Recheios e Confeitaria", "Queijos"]),
    ("Atacadista",          2.5, 6.20, "padrao",   ["Grãos e Cereais", "Óleos e Gorduras", "Enlatados", "Massas Secas", "Açúcares e Adoçantes", "Higiene e Limpeza", "Farinhas e Misturas"]),
]

FAMILIAS = [
    "Queijos", "Frios e Embutidos", "Leites e Cremes", "Manteigas e Margarinas",
    "Congelados Salgados", "Congelados Doces", "Massas Frescas", "Massas Secas",
    "Farinhas e Misturas", "Açúcares e Adoçantes", "Chocolates e Coberturas",
    "Recheios e Confeitaria", "Óleos e Gorduras", "Molhos e Condimentos",
    "Conservas", "Grãos e Cereais", "Enlatados", "Bebidas Frias",
    "Bebidas Quentes", "Sucos e Polpas", "Sorvetes e Gelados", "Descartáveis",
    "Higiene e Limpeza", "Embalagens", "Proteínas Congeladas",
]

LINHAS = [
    "Linha Básica", "Linha Premium", "Linha Food Service", "Linha Econômica",
    "Linha Artesanal", "Linha Tradicional", "Linha Leve", "Linha Profissional",
]  # 25 famílias x 8 linhas = 200 categorias

# Municípios fictícios (nenhum corresponde a cidade brasileira real).
CIDADES = [
    "Alto Sereno", "Barra do Piraquê", "Bela Corrente", "Campo Ferrugem",
    "Cravinho do Sul", "Encosta Nova", "Figueira Branca", "Fonte Limeira",
    "Guaraúna", "Ipê Sereno", "Itaquerê", "Jaraguandu", "Lagoa Vermelha do Norte",
    "Marimbondo", "Monte Cravo", "Nova Aurora do Vale", "Olho d'Água Grande",
    "Palmeira Torta", "Passo Ruivo", "Pedra Molhada", "Piraquara do Oeste",
    "Porto Bandeira", "Praia do Sino", "Quatro Cruzes", "Ribeira Funda",
    "Riozinho Claro", "Santa Ilha", "São Braz do Campo", "Serra Cansada",
    "Sertão Novo", "Tabuleiro Alto", "Taquaruçu do Meio", "Três Bicas",
    "Umbuzeiro", "Vale Formoso do Norte", "Várzea Comprida", "Vila Aurora",
    "Vila Cascata", "Vista Alegre do Ipê", "Xambrê",
]

PREFIXO_NOME = {
    "Padaria": ["Padaria", "Panificadora", "Padaria e Confeitaria"],
    "Minimercado": ["Minimercado", "Mercadinho", "Mercado"],
    "Restaurante": ["Restaurante", "Cantina", "Casa de Refeições"],
    "Lanchonete": ["Lanchonete", "Lanches", "Ponto do Lanche"],
    "Pizzaria": ["Pizzaria", "Pizza"],
    "Supermercado": ["Supermercado", "Super"],
    "Cafeteria": ["Cafeteria", "Café", "Casa do Café"],
    "Hamburgueria": ["Hamburgueria", "Burger"],
    "Sorveteria": ["Sorveteria", "Gelateria"],
    "Açaiteria": ["Açaiteria", "Açaí"],
    "Loja de Conveniência": ["Conveniência", "Loja"],
    "Buffet e Eventos": ["Buffet", "Eventos"],
    "Atacadista": ["Atacado", "Atacadão", "Distribuidora"],
}

FANTASIA = [
    "Aurora", "Bandeirante", "Bela Vista", "Bom Retiro", "Brisa", "Candeia",
    "Capim Doce", "Cascata", "Central", "Cerrado", "Chapada", "Cravo",
    "Doce Lar", "Dois Irmãos", "Encanto", "Esperança", "Estrela", "Farol",
    "Figueira", "Flor de Sal", "Fonte Nova", "Girassol", "Guarani", "Horizonte",
    "Ipanema Velha", "Ipê Roxo", "Jacarandá", "Jatobá", "Juriti", "Lagoa Azul",
    "Luar", "Maracanã Velho", "Maré Alta", "Mirante", "Moinho", "Nascente",
    "Nova Era", "Olaria", "Oliveira", "Orquídea", "Ouro Fino", "Palmeira",
    "Paraíso", "Pedra Alta", "Pequi", "Pinheiro", "Pitanga", "Ponte Velha",
    "Porto Seguro Velho", "Primavera", "Quintal", "Raiz Forte", "Recanto",
    "Rio Claro", "Rosa dos Ventos", "Sabor Real", "Santa Clara", "São Jorge",
    "Sempre Viva", "Serrana", "Sol Nascente", "Sossego", "Tamarindo",
    "Terra Boa", "Tijuca Velha", "Trigal", "Tucano", "Umbu", "Vale Verde",
    "Varanda", "Veredas", "Vila Nova", "Vitória Régia", "Xodó",
]

NOMES_PESSOA = [
    "Adriana", "Alexandre", "Aline", "Amanda", "André", "Antônio", "Beatriz",
    "Bruno", "Camila", "Carlos", "Caroline", "César", "Cláudia", "Daniel",
    "Danilo", "Débora", "Diego", "Eduardo", "Elaine", "Emerson", "Fábio",
    "Fernanda", "Flávio", "Gabriel", "Geraldo", "Gisele", "Gustavo", "Helena",
    "Igor", "Isabela", "Jaqueline", "Joana", "João", "Jorge", "Juliana",
    "Kelly", "Larissa", "Leandro", "Letícia", "Lucas", "Luciana", "Marcelo",
    "Márcia", "Marcos", "Mariana", "Mauro", "Michele", "Murilo", "Natália",
    "Nelson", "Otávio", "Patrícia", "Paulo", "Rafael", "Raquel", "Renata",
    "Ricardo", "Roberta", "Rodrigo", "Rogério", "Sabrina", "Samuel", "Sandra",
    "Sérgio", "Silvana", "Simone", "Tatiane", "Thiago", "Vanessa", "Vinícius",
    "Viviane", "Wagner", "Wesley", "Yara",
]

SOBRENOMES = [
    "Almeida", "Alves", "Andrade", "Araújo", "Barbosa", "Barros", "Batista",
    "Bezerra", "Braga", "Camargo", "Cardoso", "Carvalho", "Castro", "Cavalcanti",
    "Coelho", "Correia", "Costa", "Cunha", "Dias", "Duarte", "Faria",
    "Fernandes", "Ferreira", "Fonseca", "Freitas", "Gomes", "Gonçalves",
    "Guimarães", "Lima", "Lopes", "Macedo", "Machado", "Maia", "Marques",
    "Martins", "Medeiros", "Melo", "Mendes", "Miranda", "Moraes", "Moreira",
    "Nascimento", "Neves", "Nogueira", "Nunes", "Oliveira", "Pacheco", "Peixoto",
    "Pereira", "Pinheiro", "Pinto", "Queiroz", "Ramos", "Rezende", "Ribeiro",
    "Rocha", "Rodrigues", "Sales", "Sampaio", "Santos", "Silva", "Siqueira",
    "Soares", "Sousa", "Tavares", "Teixeira", "Vasconcelos", "Vieira", "Xavier",
]

# ----------------------------------------------------------------------------
# Casos plantados — o que a metodologia precisa achar
# ----------------------------------------------------------------------------

CASOS_PLANTADOS = {
    # sangramento silencioso: continua comprando todo mês, perde ~80% da receita
    "sangramento": 90,
    # parada súbita: anos de regularidade e some do nada
    "parada": 110,
    # perda de mix: volume parecido, larga metade das categorias
    "mix": 80,
    # sumiu e voltou: 6+ meses ausente, retorna no último mês
    "voltou": 45,
    # queda em degrau: corte de portfólio num mês só, patamar novo mais baixo
    "degrau": 70,
}

# ----------------------------------------------------------------------------
# Geração
# ----------------------------------------------------------------------------


def dirichlet(rng: random.Random, n: int, alpha: float) -> list[float]:
    """Split aleatório em n partes que somam 1 (Dirichlet simétrica)."""
    g = [rng.gammavariate(alpha, 1.0) for _ in range(n)]
    s = sum(g) or 1.0
    return [x / s for x in g]


def monta_categorias() -> list[dict]:
    cats = []
    cid = 0
    for fam in FAMILIAS:
        for linha in LINHAS:
            cats.append({"categoria_id": cid, "categoria": f"{fam} — {linha}",
                          "familia": fam, "linha": linha})
            cid += 1
    return cats


def monta_equipe(rng: random.Random) -> tuple[list[dict], list[int]]:
    """Vendedores com carteiras de CARTEIRA_MIN..CARTEIRA_MAX PDVs, sob supervisores."""
    supervisores = []
    for i in range(N_SUPERVISORES):
        supervisores.append(f"{rng.choice(NOMES_PESSOA)} {rng.choice(SOBRENOMES)}")

    # Tamanhos com assimetria à direita, reescalados para somar N_PDV.
    brutos = []
    for _ in range(N_VENDEDORES):
        brutos.append(math.exp(rng.gauss(0.0, 0.78)))
    soma = sum(brutos)
    tamanhos = []
    for b in brutos:
        t = int(round(b / soma * N_PDV))
        tamanhos.append(max(CARTEIRA_MIN, min(CARTEIRA_MAX, t)))

    # Ajuste fino para o total bater exatamente em N_PDV.
    while sum(tamanhos) != N_PDV:
        dif = N_PDV - sum(tamanhos)
        i = rng.randrange(N_VENDEDORES)
        passo = 1 if dif > 0 else -1
        novo = tamanhos[i] + passo
        if CARTEIRA_MIN <= novo <= CARTEIRA_MAX:
            tamanhos[i] = novo

    vendedores = []
    usados = set()
    for i in range(N_VENDEDORES):
        while True:
            nome = f"{rng.choice(NOMES_PESSOA)} {rng.choice(SOBRENOMES)}"
            if nome not in usados:
                usados.add(nome)
                break
        vendedores.append({
            "vendedor_id": i,
            "vendedor": nome,
            "supervisor": supervisores[i % N_SUPERVISORES],
            "carteira": tamanhos[i],
        })
    return vendedores, tamanhos


def monta_pdvs(rng_glob: random.Random, cats: list[dict], vendedores: list[dict]) -> list[dict]:
    fam_por_nome = defaultdict(list)
    for c in cats:
        fam_por_nome[c["familia"]].append(c["categoria_id"])

    tip_nomes = [t[0] for t in TIPOLOGIAS]
    tip_pesos = [t[1] for t in TIPOLOGIAS]
    tip_info = {t[0]: t for t in TIPOLOGIAS}

    # Cada PDV a um vendedor, respeitando o tamanho da carteira.
    donos = []
    for v in vendedores:
        donos.extend([v["vendedor_id"]] * v["carteira"])
    rng_glob.shuffle(donos)

    nomes_usados = set()
    pdvs = []
    for i in range(N_PDV):
        # Um gerador próprio por PDV. Sem isso, o nº de sorteios consumidos por um
        # PDV depende do seu porte (o repertório é maior), então mexer em SIGMA_LOG
        # dessincroniza todo o fluxo aleatório seguinte e troca quem recebe qual
        # caso plantado — ou seja, cada ajuste de parâmetro re-sorteia a base
        # inteira e a "sensibilidade" medida é só ruído de realização.
        rng = random.Random(SEED * 1_000_003 + i)
        tip = rng.choices(tip_nomes, weights=tip_pesos, k=1)[0]
        _, _, mult_porte, perfil, preferidas = tip_info[tip]

        # Porte: lognormal (concentração) x multiplicador da tipologia.
        base = MEDIANA_BASE_MENSAL * math.exp(rng.gauss(0.0, SIGMA_LOG)) * mult_porte
        base = max(120.0, base)

        # Repertório de categorias cresce com o porte, mas devagar.
        n_rep = round(4.0 * (base / 1200.0) ** 0.45 * rng.uniform(0.75, 1.30))
        n_rep = max(2, min(95, n_rep))

        pesos = []
        ids = []
        for c in cats:
            ids.append(c["categoria_id"])
            pesos.append(3.2 if c["familia"] in preferidas else 0.18)
        repertorio = []
        vistos = set()
        # amostragem sem reposição, ponderada
        tentativas = 0
        while len(repertorio) < n_rep and tentativas < n_rep * 40:
            tentativas += 1
            cid = rng.choices(ids, weights=pesos, k=1)[0]
            if cid not in vistos:
                vistos.add(cid)
                repertorio.append(cid)
        share = dirichlet(rng, len(repertorio), 1.15)

        # Regime de compra
        u = rng.random()
        if u < 0.50:
            regime, periodo, p_compra = "mensal", 1, rng.uniform(0.90, 0.98)
        elif u < 0.75:
            regime, periodo, p_compra = "ciclico", rng.choice([2, 2, 3]), rng.uniform(0.85, 0.96)
        else:
            regime, periodo, p_compra = "esporadico", 1, rng.uniform(0.16, 0.42)

        # Entrada na carteira: metade é legado, o resto entra ao longo dos 5 anos.
        # A entrada é uniforme no tempo de propósito: prospecção contínua é o que
        # compensa o churn contínuo e sustenta crescimento agregado positivo.
        # Se a aquisição for concentrada no início e o churn espalhado, a carteira
        # encolhe no fim da série — que foi o que aconteceu na 1ª calibração.
        entrada = 0 if rng.random() < FRACAO_LEGADO else rng.randint(1, N_MESES - 4)

        nome_base = f"{rng.choice(PREFIXO_NOME[tip])} {rng.choice(FANTASIA)}"
        nome = nome_base
        suf = 2
        while nome in nomes_usados:
            nome = f"{nome_base} {suf}"
            suf += 1
        nomes_usados.add(nome)

        pdvs.append({
            "pdv_id": i,
            "pdv": nome,
            "cidade": rng.choice(CIDADES),
            "tipologia": tip,
            "vendedor_id": donos[i],
            "base_mensal": base,
            "perfil_sazonal": perfil,
            "repertorio": repertorio,
            "share": share,
            "regime": regime,
            "periodo": periodo,
            "fase": rng.randrange(4),
            "p_compra": p_compra,
            "entrada": entrada,
            "drift_anual": max(-0.34, min(0.50, rng.gauss(0.0, DRIFT_SIGMA))),
            "caso": "normal",
            "caso_inicio": None,
            "fim": None,
            "zeros_fantasma": False,
        })
    return pdvs


def atribui_casos(rng: random.Random, pdvs: list[dict]) -> dict:
    """Distribui casos plantados e atrito natural entre PDVs elegíveis."""
    M = N_MESES - 1  # 59 = mês de referência

    # Elegível = está na base desde o começo (para ter história comparável).
    elegiveis = [p for p in pdvs if p["entrada"] <= 12]
    rng.shuffle(elegiveis)
    fila = iter(elegiveis)

    def proximo():
        while True:
            p = next(fila)
            if p["caso"] == "normal":
                return p

    # --- caso-estrela: sangramento num PDV grande, para a história do ranking.
    # Escolhido entre os maiores para que a queda de posição no ranking apareça.
    grandes = sorted([p for p in elegiveis if p["entrada"] == 0],
                     key=lambda p: -p["base_mensal"])
    heroi = grandes[127] if len(grandes) > 130 else grandes[len(grandes) // 30]
    heroi["caso"] = "sangramento"
    heroi["caso_inicio"] = M - 11        # decai só dentro da janela atual de 12m
    heroi["caso_alvo"] = 0.09
    heroi["caso_rampa"] = 2
    heroi["regime"] = "mensal"
    heroi["p_compra"] = 0.99
    heroi["heroi"] = True

    plantados = defaultdict(list)
    plantados["sangramento"].append(heroi["pdv_id"])

    # --- sangramento silencioso
    # Só em PDV com faturamento relevante, e por dois motivos. O primeiro é que
    # sangramento em conta pequena não tem R$ perdido que justifique uma visita.
    # O segundo é mecânico: um PDV minúsculo que perde 80% cai abaixo do piso de
    # emissão de linha e simplesmente PARA de aparecer na base — deixa de ser
    # sangramento silencioso e vira parada súbita, que é outro caso.
    pool_sangra = [p for p in grandes[:1200] if p["caso"] == "normal"]
    rng.shuffle(pool_sangra)
    for k in range(CASOS_PLANTADOS["sangramento"] - 1):
        if not pool_sangra:
            break
        p = pool_sangra.pop()
        p["caso"] = "sangramento"
        p["caso_inicio"] = rng.randint(M - 15, M - 7)
        p["caso_alvo"] = rng.uniform(0.12, 0.26)
        p["caso_rampa"] = rng.randint(3, 7)
        p["regime"] = "mensal"                     # continua comprando TODO mês
        p["p_compra"] = rng.uniform(0.95, 0.99)
        plantados["sangramento"].append(p["pdv_id"])

    # --- parada súbita
    for _ in range(CASOS_PLANTADOS["parada"]):
        p = proximo()
        p["caso"] = "parada"
        p["regime"] = "mensal"
        p["p_compra"] = rng.uniform(0.92, 0.99)
        p["fim"] = rng.randint(M - 13, M - 2)      # 3 a 14 meses parado
        p["caso_inicio"] = p["fim"]
        # em alguns, o ERP continua cuspindo linhas de R$ 0,00 depois da parada
        p["zeros_fantasma"] = rng.random() < 0.22
        plantados["parada"].append(p["pdv_id"])

    # --- perda de mix
    for _ in range(CASOS_PLANTADOS["mix"]):
        p = proximo()
        p["caso"] = "mix"
        p["caso_inicio"] = rng.randint(M - 12, M - 5)
        p["caso_alvo"] = rng.uniform(0.35, 0.50)   # fração do repertório que sobra
        p["regime"] = "mensal"
        p["p_compra"] = rng.uniform(0.90, 0.98)
        plantados["mix"].append(p["pdv_id"])

    # --- sumiu e voltou
    for _ in range(CASOS_PLANTADOS["voltou"]):
        p = proximo()
        p["caso"] = "voltou"
        p["regime"] = "mensal"
        p["p_compra"] = rng.uniform(0.90, 0.98)
        volta = rng.choice([M, M, M - 1])
        gap = rng.randint(4, 9)
        p["caso_inicio"] = volta - gap             # primeiro mês do sumiço
        p["caso_volta"] = volta
        p["caso_alvo"] = rng.uniform(0.35, 0.90)   # tamanho da volta
        plantados["voltou"].append(p["pdv_id"])

    # --- queda em degrau (corte de portfólio num mês só)
    for _ in range(CASOS_PLANTADOS["degrau"]):
        p = proximo()
        p["caso"] = "degrau"
        p["caso_inicio"] = rng.randint(M - 13, M - 5)
        p["caso_alvo"] = rng.uniform(0.32, 0.56)
        p["regime"] = "mensal"
        p["p_compra"] = rng.uniform(0.90, 0.98)
        plantados["degrau"].append(p["pdv_id"])

    # --- atrito natural: PDVs que somem em qualquer ponto dos 5 anos.
    # É o que povoa a fila de reativação (6-11 e 12+ meses parados). Aqui o sorteio
    # é sobre a base toda, não só sobre os elegíveis: PDV que entrou no ano 3 e
    # sumiu no ano 4 é caso comum. Só os casos de SINAL precisam de história longa.
    # O topo fica fora do sorteio de atrito. Numa carteira Pareto o 1% maior
    # carrega ~25% da receita, então deixar o atrito ALEATÓRIO sortear a 3ª maior
    # conta faz o crescimento agregado oscilar vários pontos entre realizações —
    # e distribuidor não perde conta-chave por acaso, perde por algo que se vê
    # chegando. Sangramento em conta grande continua existindo, mas plantado.
    corte_topo = sorted((p["base_mensal"] for p in pdvs), reverse=True)[int(0.01 * len(pdvs))]
    candidatos_atrito = [p for p in pdvs
                         if p["caso"] == "normal" and p["entrada"] <= N_MESES - 12
                         and p["base_mensal"] < corte_topo]
    rng.shuffle(candidatos_atrito)
    n_atrito = int(FRACAO_ATRITO * len(pdvs))
    for p in candidatos_atrito[:n_atrito]:
        p["caso"] = "atrito"
        # O atrito é puxado para o passado de propósito. Ele precisa derrubar a
        # contagem de PDVs ATIVOS (independente de quando aconteceu), mas só
        # contamina a comparação 12m x 12m se cair dentro dela. Concentrar o
        # atrito no passado deixa o YoY limpo e ainda enche a fila de reativação
        # de 12+ meses; as paradas recentes vêm dos casos plantados de "parada".
        lim = N_MESES - 12
        if rng.random() < 0.15:
            p["fim"] = rng.randint(lim, N_MESES - 6)
        else:
            piso = p["entrada"] + 5
            p["fim"] = piso + int((max(piso, lim) - piso) * rng.random() ** 1.7)
        plantados["atrito"].append(p["pdv_id"])

    return dict(plantados)


def fator_caso(p: dict, t: int) -> float | None:
    """Multiplicador do caso plantado no mês t. None = não compra nesse mês."""
    caso = p["caso"]
    ini = p["caso_inicio"]

    if p["fim"] is not None and t >= p["fim"]:
        return None

    if caso in ("normal", "atrito", "parada"):
        return 1.0

    if caso == "sangramento":
        if t < ini:
            return 1.0
        alvo, rampa = p["caso_alvo"], p["caso_rampa"]
        avanco = min(1.0, (t - ini + 1) / rampa)
        return math.exp(math.log(1.0) * (1 - avanco) + math.log(alvo) * avanco)

    if caso == "degrau":
        return 1.0 if t < ini else p["caso_alvo"]

    if caso == "mix":
        # volume quase igual; o estrago é no repertório (tratado em categorias)
        return 1.0 if t < ini else 0.94

    if caso == "voltou":
        if ini <= t < p["caso_volta"]:
            return None
        return p["caso_alvo"] if t >= p["caso_volta"] else 1.0

    return 1.0


def repertorio_no_mes(p: dict, t: int) -> tuple[list[int], list[float]]:
    """Repertório vigente — encolhe nos casos que largam categorias."""
    rep, sh = p["repertorio"], p["share"]
    caso = p["caso"]
    if caso == "mix" and t >= p["caso_inicio"]:
        n = max(1, int(round(len(rep) * p["caso_alvo"])))
        return rep[:n], sh[:n]
    if caso == "sangramento" and t >= p["caso_inicio"]:
        # sangrar também derruba cauda de categorias, só não a ponto de virar mix
        avanco = min(1.0, (t - p["caso_inicio"] + 1) / max(1, p["caso_rampa"]))
        n = max(1, int(round(len(rep) * (1 - 0.30 * avanco))))
        return rep[:n], sh[:n]
    if caso == "degrau" and t >= p["caso_inicio"]:
        n = max(1, int(round(len(rep) * 0.72)))
        return rep[:n], sh[:n]
    return rep, sh


def compra_no_mes(rng: random.Random, p: dict, t: int) -> bool:
    if t < p["entrada"]:
        return False
    if p["regime"] == "ciclico":
        no_ciclo = ((t - p["fase"]) % p["periodo"]) == 0
        return rng.random() < (p["p_compra"] if no_ciclo else 0.07)
    return rng.random() < p["p_compra"]


def gera_vendas(pdvs: list[dict]) -> list[tuple]:
    saz = {"padrao": SAZONAL_PADRAO, "verao": SAZONAL_VERAO, "eventos": SAZONAL_EVENTOS}
    linhas = []
    for p in pdvs:
        rng = random.Random(SEED * 7_919 + p["pdv_id"])   # ver nota em monta_pdvs
        indice = saz[p["perfil_sazonal"]]
        for t in range(N_MESES):
            if t < p["entrada"]:
                continue

            fc = fator_caso(p, t)

            # linhas fantasma de R$ 0,00 depois da parada — a pegadinha do ERP
            if fc is None or not compra_no_mes(rng, p, t):
                fantasma = p["zeros_fantasma"] and fc is None and rng.random() < 0.45
                if fantasma or (fc is not None and rng.random() < PROB_MES_FANTASMA):
                    rep, _ = repertorio_no_mes(p, t)
                    for cid in rng.sample(rep, k=min(len(rep), rng.randint(1, 2))):
                        linhas.append((p["pdv_id"], t, cid, 0.0))
                continue

            cresc = CRESCIMENTO_ANUAL ** (t / 12.0)
            # tendência própria do PDV: linear em t, para que a média entre PDVs
            # fique exatamente 1 e não injete crescimento agregado por convexidade
            drift = max(0.12, 1.0 + p["drift_anual"] * t / 12.0)
            ruido = math.exp(rng.gauss(-RUIDO_SIGMA ** 2 / 2, RUIDO_SIGMA))
            valor = p["base_mensal"] * indice[mes_civil(t) - 1] * cresc * drift * fc * ruido
            if valor < 25:
                continue

            rep, sh = repertorio_no_mes(p, t)
            k = max(1, min(len(rep), int(round(len(rep) * rng.uniform(0.45, 0.92)))))
            # escolhe k categorias ponderadas pelo share histórico
            idx = list(range(len(rep)))
            escolhidos = []
            pesos = list(sh)
            for _ in range(k):
                if not idx:
                    break
                j = rng.choices(idx, weights=[pesos[i] for i in idx], k=1)[0]
                idx.remove(j)
                escolhidos.append(j)

            partes = dirichlet(rng, len(escolhidos), 1.5)
            for j, frac in zip(escolhidos, partes):
                v = round(valor * frac, 2)
                if v > 0:
                    linhas.append((p["pdv_id"], t, rep[j], v))

            # linha de R$ 0,00 dentro de um mês que teve compra (bonificação/troca)
            if rng.random() < PROB_LINHA_ZERO:
                fora = [c for c in rep if c not in [rep[j] for j in escolhidos]]
                if fora:
                    linhas.append((p["pdv_id"], t, rng.choice(fora), 0.0))
    return linhas


# ----------------------------------------------------------------------------
# Validação da geração
# ----------------------------------------------------------------------------


CASOS_SINAL = ("sangramento", "parada", "mix", "voltou", "degrau")


def valida(linhas: list[tuple], pdvs: list[dict], plantados: dict) -> dict:
    M = N_MESES - 1
    ult12 = set(range(M - 11, M + 1))
    ant12 = set(range(M - 23, M - 11))
    caso_por_pdv = {p["pdv_id"]: p["caso"] for p in pdvs}

    rec_pdv_12 = defaultdict(float)
    rec_pdv_ant = defaultdict(float)
    rec_mes = defaultdict(float)
    meses_com_compra = defaultdict(set)
    n_zeros = 0

    for pdv_id, t, _cid, v in linhas:
        rec_mes[t] += v
        if v == 0:
            n_zeros += 1
            continue
        meses_com_compra[pdv_id].add(t)
        if t in ult12:
            rec_pdv_12[pdv_id] += v
        elif t in ant12:
            rec_pdv_ant[pdv_id] += v

    # Pareto sobre a receita dos últimos 12 meses
    vals = sorted(rec_pdv_12.values(), reverse=True)
    total = sum(vals) or 1.0
    n = len(vals)

    def share_topo(frac):
        k = max(1, int(round(n * frac)))
        return sum(vals[:k]) / total

    # Sazonalidade observada. Precisa ser medida contra a média móvel centrada de
    # 12 meses, não contra a média do período: sem destendenciar, o crescimento de
    # ~12% a.a. vaza para dentro do índice e achata o pico de ago-dez, porque numa
    # janela ago->jul os meses do fim do ano são sempre os mais "antigos".
    razoes = defaultdict(list)
    for t in range(6, N_MESES - 6):
        janela = [rec_mes.get(k, 0.0) for k in range(t - 6, t + 6)]
        ma = sum(janela) / 12.0
        if ma > 0 and t >= 12:
            razoes[mes_civil(t)].append(rec_mes.get(t, 0.0) / ma)
    med_civil = {m: sum(v) / len(v) for m, v in razoes.items()}
    media_geral = sum(med_civil.values()) / len(med_civil)
    saz_obs = {m: med_civil[m] / media_geral for m in sorted(med_civil)}

    # Crescimento agregado ano a ano
    def soma_janela(ini, fim):
        return sum(v for t, v in rec_mes.items() if ini <= t <= fim)

    cresc = []
    for ini in range(12, M - 10, 12):
        a = soma_janela(ini - 12, ini - 1)
        b = soma_janela(ini, ini + 11)
        if a > 0:
            cresc.append(b / a - 1)

    regimes = defaultdict(int)
    for p in pdvs:
        regimes[p["regime"]] += 1

    ativos_12m = sum(1 for pid, ms in meses_com_compra.items() if ms & ult12)

    carteiras = sorted(v for v in
                       [sum(1 for p in pdvs if p["vendedor_id"] == i) for i in range(N_VENDEDORES)])

    return {
        "linhas": len(linhas),
        "linhas_valor_zero": n_zeros,
        "pdvs_cadastrados": len(pdvs),
        "pdvs_ativos_ult_12m": ativos_12m,
        "receita_ult_12m": round(total, 2),
        "pareto": {
            "top_1pct": round(share_topo(0.01), 4),
            "top_5pct": round(share_topo(0.05), 4),
            "top_10pct": round(share_topo(0.10), 4),
            "top_20pct": round(share_topo(0.20), 4),
        },
        "sazonalidade_observada": {str(m): round(v, 3) for m, v in saz_obs.items()},
        "sazonalidade_jan_fev": round((saz_obs[1] + saz_obs[2]) / 2, 3),
        "sazonalidade_ago_dez": round(sum(saz_obs[m] for m in (8, 9, 10, 11, 12)) / 5, 3),
        "crescimento_anual": [round(c, 4) for c in cresc],
        "crescimento_ult_ano": round(
            soma_janela(M - 11, M) / max(1.0, soma_janela(M - 23, M - 12)) - 1, 4),
        # "tendência" = o mesmo YoY, tirando os PDVs com caso de deterioração
        # plantado. É o crescimento que a carteira teria sem o vazamento — e é
        # esse número que está calibrado em ~12%. O YoY cheio sai menor porque o
        # vazamento plantado custa alguns pontos: é justamente o que o dashboard
        # existe para mostrar.
        "crescimento_tendencia": round(
            sum(v for pid, v in rec_pdv_12.items() if caso_por_pdv[pid] not in CASOS_SINAL)
            / max(1.0, sum(v for pid, v in rec_pdv_ant.items()
                           if caso_por_pdv[pid] not in CASOS_SINAL)) - 1, 4),
        "regimes": dict(regimes),
        "carteira_min": carteiras[0],
        "carteira_max": carteiras[-1],
        "carteira_media": round(sum(carteiras) / len(carteiras), 1),
        "casos_plantados": {k: len(v) for k, v in plantados.items()},
    }


# ----------------------------------------------------------------------------
# Escrita
# ----------------------------------------------------------------------------


def escreve(linhas, pdvs, cats, vendedores, resumo):
    os.makedirs(DIR_SAIDA, exist_ok=True)
    vend_por_id = {v["vendedor_id"]: v for v in vendedores}

    caminho = os.path.join(DIR_SAIDA, "vendas.csv.gz")
    # mtime=0 no cabeçalho gzip: sem isso o arquivo muda de hash a cada execução
    # mesmo com dados idênticos, e "mesma semente, mesma base" deixa de ser
    # verificável por checksum.
    with io.TextIOWrapper(
        gzip.GzipFile(caminho, "wb", compresslevel=9, mtime=0),
        encoding="utf-8", newline="",
    ) as fh:
        w = csv.writer(fh)
        w.writerow(["pdv_id", "mes", "categoria_id", "valor"])
        for pdv_id, t, cid, v in linhas:
            w.writerow([pdv_id, rotulo(t), cid, f"{v:.2f}"])

    with open(os.path.join(DIR_SAIDA, "amostra_vendas.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pdv_id", "mes", "categoria_id", "valor"])
        for pdv_id, t, cid, v in linhas[:3000]:
            w.writerow([pdv_id, rotulo(t), cid, f"{v:.2f}"])

    with open(os.path.join(DIR_SAIDA, "dim_pdv.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pdv_id", "pdv", "cidade", "tipologia", "vendedor", "supervisor",
                    "regime_compra", "mes_entrada", "caso_plantado", "mes_inicio_caso"])
        for p in pdvs:
            v = vend_por_id[p["vendedor_id"]]
            ini = p["caso_inicio"]
            w.writerow([p["pdv_id"], p["pdv"], p["cidade"], p["tipologia"],
                        v["vendedor"], v["supervisor"], p["regime"],
                        rotulo(p["entrada"]), p["caso"],
                        rotulo(ini) if ini is not None else ""])

    with open(os.path.join(DIR_SAIDA, "dim_categoria.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["categoria_id", "categoria", "familia", "linha"])
        for c in cats:
            w.writerow([c["categoria_id"], c["categoria"], c["familia"], c["linha"]])

    with open(os.path.join(DIR_SAIDA, "dim_mes.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["indice", "mes", "ano", "mes_civil"])
        for t in range(N_MESES):
            a, m = ym(t)
            w.writerow([t, rotulo(t), a, m])

    with open(os.path.join(DIR_SAIDA, "resumo_geracao.json"), "w", encoding="utf-8") as fh:
        json.dump(resumo, fh, ensure_ascii=False, indent=2)


def main():
    rng = random.Random(SEED)
    print("gerando dimensões...")
    cats = monta_categorias()
    vendedores, _ = monta_equipe(rng)
    pdvs = monta_pdvs(rng, cats, vendedores)
    print("plantando casos...")
    plantados = atribui_casos(rng, pdvs)
    print("gerando vendas...")
    linhas = gera_vendas(pdvs)
    print(f"  {len(linhas):,} linhas")
    print("validando...")
    resumo = valida(linhas, pdvs, plantados)
    resumo["seed"] = SEED
    resumo["periodo"] = [rotulo(0), rotulo(N_MESES - 1)]
    resumo["categorias"] = len(cats)
    resumo["vendedores"] = len(vendedores)
    resumo["supervisores"] = N_SUPERVISORES
    print("escrevendo...")
    escreve(linhas, pdvs, cats, vendedores, resumo)
    print(json.dumps(resumo, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
