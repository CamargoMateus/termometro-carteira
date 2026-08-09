# -*- coding: utf-8 -*-
"""
Termômetro da Carteira — detecção de queda e churn em carteira B2B.

Lê a base gerada por `gerador_base.py` e aplica a metodologia:

  * três sinais comparando o MESMO período do ano anterior (elimina sazonalidade)
  * faixas de classificação a partir dos sinais
  * duas listas de categoria por PDV, numa régua de 12 meses mais rigorosa
  * ranking de receita 12m, geral e dentro da tipologia, com variação de posição
  * calibração por backtest: corta no passado, mede sinais, confere o desfecho
    12 meses à frente — tudo derivado da própria base

Saída: dados/payload_dashboard.json, consumido pela página.

Uso:  python termometro.py
"""

from __future__ import annotations

import csv
import gzip
import json
import os
from collections import defaultdict

DIR = os.path.dirname(os.path.abspath(__file__))
DIR_DADOS = os.path.join(DIR, "dados")

# ----------------------------------------------------------------------------
# Réguas da metodologia — todos os limiares num lugar só
# ----------------------------------------------------------------------------

LIM_RECEITA = -0.30          # sinal 1: receita caiu mais de 30%
LIM_FREQUENCIA = -2          # sinal 2: 2 meses a menos com compra
LIM_MIX = -0.25              # sinal 3: categorias distintas do 2º tri caíram >25%

EXTREMO_RECEITA = -0.50      # qualquer um destes, sozinho, já é "Sangrando"
EXTREMO_FREQUENCIA = -3
EXTREMO_MIX = -0.50

MESES_PARA_PAROU = 3         # 3+ meses sem nenhuma compra
OBSERVAR_DE, OBSERVAR_ATE = -0.30, -0.10
ESTAVEL_DE, ESTAVEL_ATE = -0.10, 0.10

# Régua das listas de categoria (12 meses, mais rigorosa que os sinais)
MIN_MESES_PARA_ABANDONO = 3  # categoria precisa ter sido comprada em 3+ meses
JANELA_RECENTE = 6           # "últimos 6 meses"

SERIE_MESES = 24             # série mensal do painel de detalhe.
#   24 e não 18: com 18 meses a janela de comparação do ano anterior (jan-jun)
#   cai parcialmente fora do gráfico, e o usuário lê "-81%" sem conseguir ver
#   contra o que. 24 meses mostram as duas janelas inteiras.
TOP_CATEGORIAS = 12          # itens por lista no payload

# Backtest: cortes usados na calibração (índices de mês)
CORTES_BACKTEST = [23, 29, 35, 41, 47]
DESFECHO_QUEDA = -0.50       # perdeu metade da receita nos 12 meses seguintes
DESFECHO_MESES_MUDOS = 6     # ou ficou os últimos 6 meses sem comprar nada


# ----------------------------------------------------------------------------
# Janelas, todas relativas ao mês de referência M
# ----------------------------------------------------------------------------
#
# Com M = julho, a janela relativa (M-6 .. M-1) É jan-jun do ano corrente e
# (M-18 .. M-13) É jan-jun do ano anterior. Definir as janelas de forma relativa
# faz o mesmo código servir ao dashboard e a cada corte do backtest, sem
# reimplementar a regra duas vezes.


def janelas(M: int) -> dict:
    return {
        "sem_atual":   (M - 6, M - 1),      # jan-jun do ano corrente
        "sem_ant":     (M - 18, M - 13),    # jan-jun do ano anterior
        "tri_atual":   (M - 3, M - 1),      # 2º trimestre do ano corrente
        "tri_ant":     (M - 15, M - 13),    # 2º trimestre do ano anterior
        "ult12":       (M - 11, M),
        "ant12":       (M - 23, M - 12),
        "ult6":        (M - JANELA_RECENTE + 1, M),
        "doze_antes":  (M - JANELA_RECENTE - 11, M - JANELA_RECENTE),
    }


def variacao(atual: float, anterior: float) -> float | None:
    """Variação relativa. None quando não há base de comparação."""
    if anterior <= 0:
        return None
    return atual / anterior - 1.0


# ----------------------------------------------------------------------------
# Leitura
# ----------------------------------------------------------------------------


def carrega() -> dict:
    idx_mes, rot_mes = {}, {}
    with open(os.path.join(DIR_DADOS, "dim_mes.csv"), encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            idx_mes[r["mes"]] = int(r["indice"])
            rot_mes[int(r["indice"])] = r["mes"]

    categorias = {}
    with open(os.path.join(DIR_DADOS, "dim_categoria.csv"), encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            categorias[int(r["categoria_id"])] = r["categoria"]

    pdvs = {}
    with open(os.path.join(DIR_DADOS, "dim_pdv.csv"), encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            pdvs[int(r["pdv_id"])] = {
                "pdv_id": int(r["pdv_id"]),
                "pdv": r["pdv"],
                "cidade": r["cidade"],
                "tipologia": r["tipologia"],
                "vendedor": r["vendedor"],
                "supervisor": r["supervisor"],
                "caso_plantado": r["caso_plantado"],
            }

    # rec[(pdv, t)] = receita;  cat[(pdv, cid, t)] = receita
    # As linhas de valor 0,00 são carregadas e ficam no fato, mas NUNCA contam
    # como "mês com compra" — é esse detalhe que infla a contagem de frequência
    # se a régua for ingênua.
    rec = defaultdict(float)
    cat = defaultdict(float)
    # meses em que o PDV aparece no fato, mesmo que só com linha de R$ 0,00.
    # Guardado à parte para poder MEDIR o tamanho da pegadinha, não só evitá-la.
    aparece = defaultdict(set)
    linhas = zeros = 0
    with gzip.open(os.path.join(DIR_DADOS, "vendas.csv.gz"), "rt", encoding="utf-8") as fh:
        leitor = csv.reader(fh)
        next(leitor)
        for pdv_id, mes, cid, valor in leitor:
            linhas += 1
            v = float(valor)
            t = idx_mes[mes]
            aparece[int(pdv_id)].add(t)
            if v == 0.0:
                zeros += 1
                continue
            rec[(int(pdv_id), t)] += v
            cat[(int(pdv_id), int(cid), t)] += v

    return {"idx_mes": idx_mes, "rot_mes": rot_mes, "categorias": categorias,
            "pdvs": pdvs, "rec": rec, "cat": cat, "aparece": aparece,
            "n_meses": len(idx_mes), "n_linhas": linhas, "n_zeros": zeros}


def indexa(base: dict) -> dict:
    """Reorganiza em estruturas por PDV, que é como as réguas consultam."""
    serie = defaultdict(dict)          # pdv -> {t: receita}
    cats_por_mes = defaultdict(dict)   # pdv -> {t: set(cid)}
    cat_serie = defaultdict(dict)      # (pdv, cid) -> {t: receita}

    for (pdv, t), v in base["rec"].items():
        serie[pdv][t] = v
    for (pdv, cid, t), v in base["cat"].items():
        cat_serie[(pdv, cid)][t] = v
        cats_por_mes[pdv].setdefault(t, set()).add(cid)

    base["serie"] = serie
    base["cats_por_mes"] = cats_por_mes
    base["cat_serie"] = cat_serie
    return base


# ----------------------------------------------------------------------------
# Primitivas de janela
# ----------------------------------------------------------------------------


def soma(serie: dict, ini: int, fim: int) -> float:
    return sum(v for t, v in serie.items() if ini <= t <= fim)


def meses_com_compra(serie: dict, ini: int, fim: int) -> int:
    """Conta só mês com valor > 0. A série já vem sem as linhas de R$ 0,00."""
    return sum(1 for t, v in serie.items() if ini <= t <= fim and v > 0)


def categorias_distintas(cats_por_mes: dict, ini: int, fim: int) -> int:
    vistas = set()
    for t, s in cats_por_mes.items():
        if ini <= t <= fim:
            vistas |= s
    return len(vistas)


def ultimo_mes_com_compra(serie: dict, ate: int) -> int | None:
    ms = [t for t, v in serie.items() if t <= ate and v > 0]
    return max(ms) if ms else None


def primeiro_mes_com_compra(serie: dict) -> int | None:
    ms = [t for t, v in serie.items() if v > 0]
    return min(ms) if ms else None


# ----------------------------------------------------------------------------
# Os três sinais
# ----------------------------------------------------------------------------


def calcula_sinais(serie: dict, cats_por_mes: dict, M: int) -> dict:
    j = janelas(M)

    rec_atual = soma(serie, *j["sem_atual"])
    rec_ant = soma(serie, *j["sem_ant"])
    var_rec = variacao(rec_atual, rec_ant)

    freq_atual = meses_com_compra(serie, *j["sem_atual"])
    freq_ant = meses_com_compra(serie, *j["sem_ant"])
    dif_freq = freq_atual - freq_ant

    mix_atual = categorias_distintas(cats_por_mes, *j["tri_atual"])
    mix_ant = categorias_distintas(cats_por_mes, *j["tri_ant"])
    var_mix = variacao(mix_atual, mix_ant)

    # Sem base no ano anterior não se afirma queda: o sinal fica desligado.
    s_receita = var_rec is not None and var_rec < LIM_RECEITA
    s_frequencia = freq_ant > 0 and dif_freq <= LIM_FREQUENCIA
    s_mix = var_mix is not None and var_mix < LIM_MIX

    return {
        "rec_atual": rec_atual, "rec_ant": rec_ant, "var_rec": var_rec,
        "freq_atual": freq_atual, "freq_ant": freq_ant, "dif_freq": dif_freq,
        "mix_atual": mix_atual, "mix_ant": mix_ant, "var_mix": var_mix,
        "s_receita": s_receita, "s_frequencia": s_frequencia, "s_mix": s_mix,
        "n_sinais": int(s_receita) + int(s_frequencia) + int(s_mix),
        "sem_base": rec_ant <= 0,
    }


def extremo(s: dict) -> bool:
    """Um único sinal, mas em nível que já não admite dúvida."""
    return ((s["var_rec"] is not None and s["var_rec"] <= EXTREMO_RECEITA)
            or (s["freq_ant"] > 0 and s["dif_freq"] <= EXTREMO_FREQUENCIA)
            or (s["var_mix"] is not None and s["var_mix"] <= EXTREMO_MIX))


def mede_atraso(serie: dict, M: int) -> tuple[int, int | None]:
    """(meses sem comprar, último mês com compra)."""
    ult = ultimo_mes_com_compra(serie, M)
    if ult is None:
        return 999, None
    return M - ult, ult


def voltou_a_comprar(serie: dict, M: int) -> tuple[bool, int]:
    """
    Retornou depois de 3+ meses sumido?  Olha para trás a partir de M: mede o
    tamanho da sequência ativa que termina agora e o buraco imediatamente
    anterior a ela. Sem isso, quem voltou seria classificado como sangrando —
    e o vendedor receberia um alerta de queda sobre um cliente que acabou de
    voltar a comprar.
    """
    ativo = {t for t, v in serie.items() if v > 0}
    if M not in ativo and (M - 1) not in ativo:
        return False, 0

    t = M
    while t >= 0 and t not in ativo:      # tolera o mês corrente ainda vazio
        t -= 1
    fim_streak = t
    while t >= 0 and t in ativo:
        t -= 1
    ini_streak = t + 1

    buraco = 0
    while t >= 0 and t not in ativo:
        buraco += 1
        t -= 1

    streak = fim_streak - ini_streak + 1
    return (buraco >= MESES_PARA_PAROU and streak <= 3 and t >= 0), buraco


def classifica(serie: dict, cats_por_mes: dict, M: int) -> dict:
    s = calcula_sinais(serie, cats_por_mes, M)
    atraso, ult = mede_atraso(serie, M)
    primeiro = primeiro_mes_com_compra(serie)
    novo = primeiro is not None and primeiro > M - 18

    voltou, buraco = voltou_a_comprar(serie, M)

    if atraso >= MESES_PARA_PAROU:
        faixa = "parou"
    elif voltou:
        faixa = "voltou"
    elif novo and s["sem_base"]:
        faixa = "novo"
    elif s["n_sinais"] >= 2 or (s["n_sinais"] >= 1 and extremo(s)):
        faixa = "sangrando"
    elif s["n_sinais"] == 1:
        faixa = "degradando"
    elif s["var_rec"] is None:
        faixa = "novo" if novo else "estavel"
    elif OBSERVAR_DE <= s["var_rec"] < OBSERVAR_ATE:
        faixa = "observar"
    elif s["var_rec"] < OBSERVAR_DE:
        faixa = "observar"      # queda forte sem sinal disparado: raro, mas existe
    elif ESTAVEL_DE <= s["var_rec"] <= ESTAVEL_ATE:
        faixa = "estavel"
    else:
        faixa = "crescendo"

    s.update({"faixa": faixa, "atraso": atraso, "ultimo_mes": ult,
              "primeiro_mes": primeiro, "buraco": buraco, "novo": novo})
    return s


# ----------------------------------------------------------------------------
# As duas listas de categoria (régua de 12 meses)
# ----------------------------------------------------------------------------


def listas_de_categoria(pdv: int, base: dict, M: int) -> tuple[list, list]:
    """
    "Parou de comprar": categoria comprada em 3+ meses DISTINTOS nos 12 meses
    anteriores e zero nos últimos 6. O filtro de 3+ meses é o que impede compra
    sazonal avulsa de virar falso abandono.

    "Continua comprando": categoria com compra nos últimos 6 meses.

    A régua aqui é mais rigorosa que a dos sinais de propósito: serve para o
    vendedor ligar para o cliente, e não se cobra uma categoria que ele comprou
    mês passado.
    """
    j = janelas(M)
    r6_ini, r6_fim = j["ult6"]
    d12_ini, d12_fim = j["doze_antes"]
    u12_ini, u12_fim = j["ult12"]

    paradas, ativas = [], []
    for (p, cid), serie in base["cat_serie"].items():
        if p != pdv:
            continue

        meses_antes = [t for t, v in serie.items() if d12_ini <= t <= d12_fim and v > 0]
        meses_recentes = [t for t, v in serie.items() if r6_ini <= t <= r6_fim and v > 0]

        if meses_recentes:
            ativas.append({
                "cid": cid,
                "valor": round(soma(serie, u12_ini, u12_fim), 2),
                "meses": len(meses_recentes),   # x de 6
            })
        elif len(meses_antes) >= MIN_MESES_PARA_ABANDONO:
            paradas.append({
                "cid": cid,
                # valor real dos 12 meses anteriores, sem anualizar
                "valor": round(soma(serie, d12_ini, d12_fim), 2),
                "ultimo": max(meses_antes),
                "meses": len(meses_antes),
            })

    paradas.sort(key=lambda x: -x["valor"])
    ativas.sort(key=lambda x: -x["valor"])
    return paradas, ativas


# ----------------------------------------------------------------------------
# Ranking e faixa de porte
# ----------------------------------------------------------------------------

FAIXAS_PORTE = [(0.01, "top1"), (0.05, "top5"), (0.10, "top10"),
                (0.25, "top25"), (0.50, "top50"), (1.01, "cauda")]


def faixa_porte(posicao: int, n: int) -> str:
    r = posicao / max(1, n)
    for lim, nome in FAIXAS_PORTE:
        if r <= lim:
            return nome
    return "cauda"


def monta_ranking(receitas: dict[int, float]) -> dict[int, int]:
    """Posição 1..n por receita decrescente, só entre quem teve receita > 0."""
    ordenado = sorted((p for p, v in receitas.items() if v > 0),
                      key=lambda p: -receitas[p])
    return {p: i + 1 for i, p in enumerate(ordenado)}


# ----------------------------------------------------------------------------
# Calibração por backtest
# ----------------------------------------------------------------------------


def ritmo_de_compra(n_meses: int) -> str:
    if n_meses >= 11:
        return "todo_mes"
    if n_meses >= 8:
        return "quase_todo_mes"
    if n_meses >= 4:
        return "ciclico"
    return "esporadico"


def faixa_atraso(atraso: int) -> str:
    if atraso <= 0:
        return "0"
    if atraso == 1:
        return "1"
    if atraso == 2:
        return "2"
    if atraso <= 5:
        return "3-5"
    return "6+"


def desfecho_ruim(serie: dict, c: int) -> bool | None:
    """
    Olha 12 meses à frente do corte c. Desfecho ruim = perdeu metade da receita
    OU ficou os últimos 6 meses da janela sem comprar nada.
    """
    if c + 12 > MAX_MES:
        return None
    antes = soma(serie, c - 11, c)
    depois = soma(serie, c + 1, c + 12)
    if antes <= 0:
        return None
    if meses_com_compra(serie, c + 12 - DESFECHO_MESES_MUDOS + 1, c + 12) == 0:
        return True
    return (depois / antes - 1.0) <= DESFECHO_QUEDA


def backtest(base: dict) -> dict:
    """
    Para cada corte: mede os sinais só com dados até o corte e confere o desfecho
    nos 12 meses seguintes. Nada aqui vem de fora — a calibração sai da própria
    base. Os cortes distam 6 meses, então as janelas se sobrepõem e as
    observações não são independentes; serve para ordenar risco, não para
    reivindicar precisão de terceira casa.
    """
    escada = {0: [0, 0], 1: [0, 0], 2: [0, 0], 3: [0, 0]}   # n_sinais -> [ruins, total]
    heat = defaultdict(lambda: [0, 0])                      # (ritmo, atraso) -> [ruins, total]
    n_obs = 0

    for c in CORTES_BACKTEST:
        j = janelas(c)
        for pdv, serie in base["serie"].items():
            # elegível: era cliente antes e ainda aparecia na janela recente
            if soma(serie, *j["ult12"]) <= 0 or soma(serie, *j["ant12"]) <= 0:
                continue
            ruim = desfecho_ruim(serie, c)
            if ruim is None:
                continue

            s = calcula_sinais(serie, base["cats_por_mes"].get(pdv, {}), c)
            escada[s["n_sinais"]][1] += 1
            escada[s["n_sinais"]][0] += int(ruim)

            atraso, _ = mede_atraso(serie, c)
            ritmo = ritmo_de_compra(meses_com_compra(serie, *j["ult12"]))
            k = (ritmo, faixa_atraso(atraso))
            heat[k][1] += 1
            heat[k][0] += int(ruim)
            n_obs += 1

    # Grade cheia, inclusive as células que a definição torna impossíveis: quem
    # comprou em 11 dos 12 meses não pode estar 3 meses sem comprar. Célula vazia
    # é "não existe", e é diferente de "existe e o risco é zero" — a página
    # precisa poder distinguir as duas.
    grade = []
    for ritmo in ("todo_mes", "quase_todo_mes", "ciclico", "esporadico"):
        for atr in ("0", "1", "2", "3-5", "6+"):
            r, t = heat.get((ritmo, atr), [0, 0])
            grade.append({"ritmo": ritmo, "atraso": atr, "ruins": r, "total": t,
                          "taxa": round(r / t, 4) if t else None})

    return {
        "escada": [{"sinais": k, "ruins": v[0], "total": v[1],
                    "taxa": round(v[0] / v[1], 4) if v[1] else None}
                   for k, v in sorted(escada.items())],
        "heatmap": grade,
        "cortes": [ROTULOS[c] for c in CORTES_BACKTEST],
        "observacoes": n_obs,
        "definicao_desfecho": (
            f"receita dos 12 meses seguintes ao corte caiu {int(-DESFECHO_QUEDA*100)}% "
            f"ou mais contra os 12 anteriores, ou o PDV passou os últimos "
            f"{DESFECHO_MESES_MUDOS} meses da janela sem nenhuma compra"),
    }


# ----------------------------------------------------------------------------
# Montagem do payload
# ----------------------------------------------------------------------------

ROTULOS: dict[int, str] = {}
MAX_MES = 0

ROTULO_FAIXA = {
    "parou": "Parou", "sangrando": "Sangrando", "degradando": "Degradando",
    "observar": "Observar", "estavel": "Estável", "crescendo": "Crescendo",
    "voltou": "Voltou", "novo": "Novo",
}
FAIXAS_FILA = ("parou", "sangrando", "degradando", "observar", "voltou")


def main():
    global ROTULOS, MAX_MES

    print("carregando base...")
    base = indexa(carrega())
    ROTULOS = base["rot_mes"]
    MAX_MES = base["n_meses"] - 1
    M = MAX_MES
    j = janelas(M)
    print(f"  {base['n_linhas']:,} linhas ({base['n_zeros']:,} com valor R$ 0,00)")
    print(f"  mês de referência: {ROTULOS[M]}")

    pdvs = base["pdvs"]
    serie_de = base["serie"]

    # --- receitas de janela e ranking
    rec12 = {p: soma(serie_de.get(p, {}), *j["ult12"]) for p in pdvs}
    rec_ant12 = {p: soma(serie_de.get(p, {}), *j["ant12"]) for p in pdvs}

    rank_atual = monta_ranking(rec12)
    rank_ant = monta_ranking(rec_ant12)
    n_rank_atual, n_rank_ant = len(rank_atual), len(rank_ant)

    por_tip = defaultdict(dict)
    por_tip_ant = defaultdict(dict)
    for p, info in pdvs.items():
        por_tip[info["tipologia"]][p] = rec12[p]
        por_tip_ant[info["tipologia"]][p] = rec_ant12[p]
    rank_tip = {}
    rank_tip_ant = {}
    n_tip = {}
    for tip, d in por_tip.items():
        r = monta_ranking(d)
        n_tip[tip] = len(r)
        rank_tip.update({p: v for p, v in r.items()})
    for tip, d in por_tip_ant.items():
        rank_tip_ant.update(monta_ranking(d))

    # --- classificação
    print("classificando...")
    resultado = {}
    for p in pdvs:
        s = classifica(serie_de.get(p, {}), base["cats_por_mes"].get(p, {}), M)
        s["rec12"] = rec12[p]
        s["rec_ant12"] = rec_ant12[p]
        s["perdido"] = max(0.0, rec_ant12[p] - rec12[p])
        resultado[p] = s

    # --- fila de risco: quem ainda comprou nos últimos 12 meses e não está bem
    fila = [p for p in pdvs
            if rec12[p] > 0 and resultado[p]["faixa"] in FAIXAS_FILA]
    perdidos = [p for p in pdvs if resultado[p]["atraso"] >= MESES_PARA_PAROU
                and resultado[p]["ultimo_mes"] is not None]
    print(f"  fila de risco: {len(fila)} PDVs | perdidos: {len(perdidos)}")

    # --- detalhe (série + listas) para quem aparece em alguma tela
    detalhe_para = set(fila) | set(perdidos)
    serie_ini = M - SERIE_MESES + 1
    cat_nomes = base["categorias"]

    registros = []
    for p in sorted(detalhe_para, key=lambda x: -resultado[x]["perdido"]):
        r = resultado[p]
        info = pdvs[p]
        sr = serie_de.get(p, {})
        serie18 = [round(sr.get(t, 0.0)) for t in range(serie_ini, M + 1)]

        paradas, ativas = listas_de_categoria(p, base, M)
        reg = {
            "id": p,
            "pdv": info["pdv"],
            "cidade": info["cidade"],
            "tip": info["tipologia"],
            "vend": info["vendedor"],
            "sup": info["supervisor"],
            "faixa": r["faixa"],
            "rec12": round(r["rec12"]),
            "rec_ant12": round(r["rec_ant12"]),
            "perdido": round(r["perdido"]),
            "sem_atual": round(r["rec_atual"]),
            "sem_ant": round(r["rec_ant"]),
            "var_rec": None if r["var_rec"] is None else round(r["var_rec"], 3),
            "freq_atual": r["freq_atual"],
            "freq_ant": r["freq_ant"],
            "mix_atual": r["mix_atual"],
            "mix_ant": r["mix_ant"],
            "var_mix": None if r["var_mix"] is None else round(r["var_mix"], 3),
            "sinais": [int(r["s_receita"]), int(r["s_frequencia"]), int(r["s_mix"])],
            "n_sinais": r["n_sinais"],
            "atraso": r["atraso"] if r["atraso"] < 999 else None,
            "ult_mes": ROTULOS.get(r["ultimo_mes"]) if r["ultimo_mes"] is not None else None,
            "ritmo": ritmo_de_compra(meses_com_compra(sr, *j["ult12"])),
            # o que o PDV gerava nos 12 meses antes de parar. Para a fila de
            # reativação é este o número que importa, e não a receita dos últimos
            # 12 meses — que num PDV parado há 8 meses já está truncada.
            "gerava12": round(soma(sr, r["ultimo_mes"] - 11, r["ultimo_mes"]))
                        if r["ultimo_mes"] is not None else 0.0,
            "serie": serie18,
            "rank": rank_atual.get(p),
            "rank_ant": rank_ant.get(p),
            "rank_tip": rank_tip.get(p),
            "rank_tip_ant": rank_tip_ant.get(p),
            "n_tip": n_tip.get(info["tipologia"]),
            "porte": faixa_porte(rank_atual[p], n_rank_atual) if p in rank_atual else "cauda",
            "paradas": [[cat_nomes[c["cid"]], round(c["valor"]), ROTULOS[c["ultimo"]], c["meses"]]
                        for c in paradas[:TOP_CATEGORIAS]],
            "n_paradas": len(paradas),
            "vl_paradas": round(sum(c["valor"] for c in paradas)),
            "ativas": [[cat_nomes[c["cid"]], round(c["valor"]), c["meses"]]
                       for c in ativas[:TOP_CATEGORIAS]],
            "n_ativas": len(ativas),
        }
        registros.append(reg)

    # --- distribuição da carteira por faixa.
    # Só entre os ATIVOS (compraram algo nos últimos 12 meses). Os parados há 12+
    # meses somam mais de mil PDVs e, jogados no mesmo gráfico, viram 40% da
    # carteira e afogam tudo — eles têm tela própria, a de reativação.
    dist = defaultdict(lambda: {"n": 0, "rec12": 0.0, "perdido": 0.0})
    for p in pdvs:
        r = resultado[p]
        if r["ultimo_mes"] is None or r["rec12"] <= 0:
            continue
        d = dist[r["faixa"]]
        d["n"] += 1
        d["rec12"] += r["rec12"]
        d["perdido"] += r["perdido"]

    # --- fila de reativação, agrupada por há quanto tempo pararam
    grupos = [("3-5", 3, 5), ("6-11", 6, 11), ("12+", 12, 10**6)]
    perdidos_resumo = []
    for rot, a, b in grupos:
        alvo = [p for p in perdidos if a <= resultado[p]["atraso"] <= b]
        perdidos_resumo.append({
            "grupo": rot, "n": len(alvo),
            "gerava": round(sum(soma(serie_de.get(p, {}),
                                     resultado[p]["ultimo_mes"] - 11,
                                     resultado[p]["ultimo_mes"]) for p in alvo), 2),
        })

    # --- tamanho da pegadinha das linhas de R$ 0,00, medido nesta base
    a_sem, b_sem = j["sem_atual"]
    inflados = 0
    for p in pdvs:
        reais = meses_com_compra(serie_de.get(p, {}), a_sem, b_sem)
        ingenuo = len({t for t in base["aparece"].get(p, ()) if a_sem <= t <= b_sem})
        if ingenuo > reais:
            inflados += 1

    # --- destaques: os casos que a metodologia existe para achar
    silenciosos = [p for p in fila
                   if resultado[p]["faixa"] == "sangrando"
                   and resultado[p]["var_rec"] is not None
                   and resultado[p]["var_rec"] <= EXTREMO_RECEITA
                   and resultado[p]["dif_freq"] >= -1]
    # Só conta queda de posição de quem ERA grande. Cair da posição 1.053 para a
    # 2.484 na cauda não é notícia: lá as posições estão coladas e R$ 200 de
    # diferença já move centenas de lugares.
    com_rank = [p for p in fila if p in rank_atual and p in rank_ant
                and rank_ant[p] <= 0.10 * n_rank_ant]
    maior_queda = max(com_rank, key=lambda p: rank_atual[p] - rank_ant[p], default=None)

    destaques = {
        "silenciosos": {
            "n": len(silenciosos),
            "perdido": round(sum(resultado[p]["perdido"] for p in silenciosos), 2),
        },
        "freq_inflada": inflados,
        "maior_queda_ranking": None if maior_queda is None else {
            "pdv": pdvs[maior_queda]["pdv"],
            "de": rank_ant[maior_queda], "para": rank_atual[maior_queda],
            "posicoes": rank_atual[maior_queda] - rank_ant[maior_queda],
        },
    }

    # --- equipe
    equipe = defaultdict(lambda: {"carteira": 0, "ativos": 0, "faixas": defaultdict(int),
                                  "perdido": 0.0, "semestre": 0.0, "rec12": 0.0})
    sup_de = {}
    for p, info in pdvs.items():
        r = resultado[p]
        e = equipe[info["vendedor"]]
        sup_de[info["vendedor"]] = info["supervisor"]
        e["carteira"] += 1
        if r["rec12"] > 0:
            e["ativos"] += 1
        if r["ultimo_mes"] is not None:
            e["faixas"][r["faixa"]] += 1
        e["perdido"] += r["perdido"]
        e["semestre"] += r["rec_atual"]
        e["rec12"] += r["rec12"]

    lista_equipe = []
    for nome, e in equipe.items():
        lista_equipe.append({
            "vend": nome, "sup": sup_de[nome],
            "carteira": e["carteira"], "ativos": e["ativos"],
            "faixas": {k: v for k, v in e["faixas"].items()},
            "perdido": round(e["perdido"], 2),
            "semestre": round(e["semestre"], 2),
            "rec12": round(e["rec12"], 2),
        })
    lista_equipe.sort(key=lambda x: -x["perdido"])

    # --- calibração
    print("rodando backtest de calibração...")
    calib = backtest(base)
    print(f"  {calib['observacoes']:,} observações em {len(CORTES_BACKTEST)} cortes")
    for e in calib["escada"]:
        if e["total"]:
            print(f"    {e['sinais']} sinais: {e['taxa']:.1%} de desfecho ruim "
                  f"(n={e['total']:,})")

    # --- recall contra o gabarito dos casos plantados
    detectado = defaultdict(lambda: [0, 0])
    for p, info in pdvs.items():
        caso = info["caso_plantado"]
        if caso in ("normal",):
            continue
        achou = resultado[p]["faixa"] in ("parou", "sangrando", "degradando", "voltou")
        detectado[caso][1] += 1
        detectado[caso][0] += int(achou)
    recall = {k: {"achados": v[0], "plantados": v[1],
                  "recall": round(v[0] / v[1], 4) if v[1] else None}
              for k, v in sorted(detectado.items())}
    print("  recall por caso plantado:")
    for k, v in recall.items():
        print(f"    {k:14} {v['achados']:>4}/{v['plantados']:<4} {v['recall']:.1%}")

    with open(os.path.join(DIR_DADOS, "resumo_geracao.json"), encoding="utf-8") as fh:
        resumo_geracao = json.load(fh)

    total_rec12 = sum(rec12.values())
    total_ant12 = sum(rec_ant12.values())

    payload = {
        "meta": {
            "mes_referencia": ROTULOS[M],
            "primeiro_mes": ROTULOS[0],
            "meses_base": base["n_meses"],
            "linhas_fato": base["n_linhas"],
            "linhas_valor_zero": base["n_zeros"],
            "pdvs_cadastrados": len(pdvs),
            "pdvs_ativos_12m": sum(1 for v in rec12.values() if v > 0),
            "categorias": len(cat_nomes),
            "vendedores": len(lista_equipe),
            "supervisores": len(set(sup_de.values())),
            "receita_12m": round(total_rec12, 2),
            "receita_ant12": round(total_ant12, 2),
            "var_receita": round(total_rec12 / total_ant12 - 1, 4),
            "perdido_total": round(sum(r["perdido"] for r in resultado.values()), 2),
            "serie_meses": [ROTULOS[t] for t in range(serie_ini, M + 1)],
            "janelas": {k: [ROTULOS[a], ROTULOS[b]] for k, (a, b) in j.items()},
            "seed": resumo_geracao["seed"],
        },
        "reguas": {
            "receita": LIM_RECEITA, "frequencia": LIM_FREQUENCIA, "mix": LIM_MIX,
            "extremo_receita": EXTREMO_RECEITA,
            "extremo_frequencia": EXTREMO_FREQUENCIA,
            "extremo_mix": EXTREMO_MIX,
            "meses_para_parou": MESES_PARA_PAROU,
            "min_meses_abandono": MIN_MESES_PARA_ABANDONO,
            "janela_recente": JANELA_RECENTE,
        },
        "faixas": [{"id": k, "rotulo": ROTULO_FAIXA[k], "n": v["n"],
                    "rec12": round(v["rec12"], 2), "perdido": round(v["perdido"], 2)}
                   for k, v in sorted(dist.items(), key=lambda x: -x[1]["n"])],
        "tipologias": sorted({i["tipologia"] for i in pdvs.values()}),
        "supervisores": sorted(set(sup_de.values())),
        "perdidos_resumo": perdidos_resumo,
        "destaques": destaques,
        "pdvs": registros,
        "equipe": lista_equipe,
        "calibracao": calib,
        "validacao": {"recall": recall, "geracao": resumo_geracao},
    }

    caminho = os.path.join(DIR_DADOS, "payload_dashboard.json")
    with open(caminho, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    kb = os.path.getsize(caminho) / 1024
    print(f"\npayload: {caminho}  ({kb:,.0f} KB, {len(registros)} PDVs com detalhe)")

    print("\ndistribuição da carteira:")
    for f in payload["faixas"]:
        print(f"  {f['rotulo']:12} {f['n']:>5}  R$ {f['rec12']/1e6:>7,.1f}M  "
              f"perdido R$ {f['perdido']/1e6:>6,.1f}M")


if __name__ == "__main__":
    main()
