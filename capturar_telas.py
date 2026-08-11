# -*- coding: utf-8 -*-
"""
Captura as telas do painel em PNG 16:9, usando o Chrome em modo headless.

Serve para o portfólio (o Workana pede proporção 1:1 / 16:9 / 4:3) e como
verificação visual: nenhuma checagem programática de layout pega uma barra de
largura zero ou um rótulo colidindo — para isso é preciso olhar a imagem.

Cada aba do painel tem endereço próprio (#fila, #geral, ...), o que permite
capturar tela por tela sem depender de clique.

Uso:  python capturar_telas.py
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
SAIDA = os.path.join(DIR, "capturas")
PAGINA = "file:///" + os.path.join(DIR, "index.html").replace("\\", "/")

LARGURA, ALTURA = 1600, 900          # 16:9

CANDIDATOS_CHROME = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

TELAS = [
    ("1-fila-de-risco", "fila",
     "Fila de risco com o detalhe de um PDV aberto — a tela que conta a história inteira"),
    ("2-visao-geral", "geral",
     "Distribuição da carteira e a escada de calibração"),
    ("3-metodologia", "metodo",
     "O texto que explica como cada régua é calculada e por quê"),
    ("4-equipe", "equipe",
     "R$ perdido concentrado por vendedor"),
    ("5-perdidos", "perdidos",
     "Fila de reativação, agrupada por tempo parado"),
]


def acha_chrome() -> str:
    for c in CANDIDATOS_CHROME:
        if os.path.isfile(c):
            return c
    raise SystemExit("Chrome/Edge não encontrado — ajuste CANDIDATOS_CHROME.")


def dimensoes_png(caminho: str) -> tuple[int, int]:
    with open(caminho, "rb") as fh:
        cab = fh.read(24)
    return struct.unpack(">II", cab[16:24])


def main():
    chrome = acha_chrome()
    os.makedirs(SAIDA, exist_ok=True)
    for velho in os.listdir(SAIDA):
        if velho.endswith(".png"):
            os.remove(os.path.join(SAIDA, velho))

    print(f"chrome: {chrome}")
    print(f"página: {PAGINA}\n")
    total = 0
    for nome, aba, descricao in TELAS:
        destino = os.path.join(SAIDA, nome + ".png")
        cmd = [
            chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1",
            # dá tempo do JavaScript montar a tela antes do disparo
            "--virtual-time-budget=8000",
            f"--window-size={LARGURA},{ALTURA}",
            f"--screenshot={destino}",
            f"{PAGINA}#{aba}",
        ]
        subprocess.run(cmd, capture_output=True, timeout=120)
        if not os.path.exists(destino):
            print(f"  FALHOU  {nome}")
            continue
        w, h = dimensoes_png(destino)
        kb = os.path.getsize(destino) / 1024
        total += kb
        marca = "ok" if abs(w / h - 16 / 9) < 0.01 else f"PROPORÇÃO {w/h:.2f}"
        print(f"  {marca:4} {nome:18} {w}x{h}  {kb:>5.0f} KB  — {descricao}")

    print(f"\n  total {total/1024:.2f} MB em {SAIDA}")
    if total / 1024 > 9:
        print("  atenção: acima de 9 MB, o upload em lote do Workana tem teto de 10 MB")


if __name__ == "__main__":
    sys.exit(main())
