# -*- coding: utf-8 -*-
"""
Injeta o payload da análise no template e escreve index.html.

O template fica separado para poder ser editado como HTML de verdade, com
realce de sintaxe; o build só troca o marcador __PAYLOAD__ pelo JSON.

Uso:  python build_dashboard.py
"""

from __future__ import annotations

import json
import os

DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(DIR, "dashboard_template.html")
PAYLOAD = os.path.join(DIR, "dados", "payload_dashboard.json")
# index.html na raiz: é o que o GitHub Pages serve sem configuração nenhuma,
# e deixa o link do portfólio limpo (usuario.github.io/termometro-carteira).
SAIDA = os.path.join(DIR, "index.html")

MARCADOR = "__PAYLOAD__"


def main():
    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    if MARCADOR not in html:
        raise SystemExit(f"marcador {MARCADOR} não encontrado no template")

    with open(PAYLOAD, encoding="utf-8") as fh:
        bruto = fh.read()
    json.loads(bruto)   # falha cedo se o payload estiver corrompido

    # O JSON vai dentro de <script type="application/json">, então a única
    # sequência que precisa escapar é a que fecharia a tag antes da hora.
    seguro = bruto.replace("</", "<\\/")

    html = html.replace(MARCADOR, seguro)
    with open(SAIDA, "w", encoding="utf-8") as fh:
        fh.write(html)

    kb = os.path.getsize(SAIDA) / 1024
    print(f"{SAIDA}  ({kb:,.0f} KB)")


if __name__ == "__main__":
    main()
