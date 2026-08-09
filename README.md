# Termômetro da Carteira

Detecção de queda e churn numa carteira B2B de distribuição alimentar: quais pontos
de venda estão perdendo receita, quais pararam de comprar, e — o caso mais caro —
quais continuam comprando **todo mês** enquanto entregam metade do que entregavam.

Dashboard de uma página, sem dependências, sobre uma base **100% sintética** gerada
por script. Nenhum número vem de cliente real.

**▶ Ver o painel: [camargomateus.github.io/termometro-carteira](https://camargomateus.github.io/termometro-carteira/)**

```bash
python gerador_base.py      # gera a base       (~8 s)
python termometro.py        # aplica a metodologia (~6 s)
python build_dashboard.py   # monta index.html
```

Python 3.10+, biblioteca padrão apenas. Sem numpy, sem pandas, sem npm.

---

## Por que gerar a base em vez de anonimizar uma real

Anonimizar troca os rótulos e mantém os números — o que continua sendo dado do
cliente, e ainda quebra: soma mensal deixa de fechar com o total do semestre,
ranking não bate com receita. Gerando a base, **todos os números fecham por
construção** e a peça pode ser mostrada sem ressalva. O gerador virou parte do
trabalho, não um rodapé dele.

A base tem as propriedades que fazem a metodologia ser necessária, calibradas e
verificadas a cada execução (`dados/resumo_geracao.json`):

| Propriedade | Alvo | Medido |
|---|---|---|
| Concentração — top 1% da receita | ~25% | 25,3% |
| Concentração — top 10% | ~65% | 64,9% |
| Concentração — top 20% | ~80% | 79,8% |
| Sazonalidade — jan/fev vs mês médio | ~80% | 82% |
| Sazonalidade — ago-dez vs mês médio | ~110% | 109% |
| Crescimento de tendência ao ano | ~12% | 12,6% |
| PDVs ativos nos últimos 12 meses | ~3.000 | 3.025 |

Escala: 4.200 PDVs cadastrados em 5 anos, 200 categorias, 90 vendedores sob 7
supervisores, ~421 mil linhas de venda mensal no grão (PDV, mês, categoria).

### Três coisas que a calibração ensinou

**Os alvos de Pareto caem de um único parâmetro.** Para uma lognormal, a fração da
receita no topo `q` é `1 − Φ(Φ⁻¹(1−q) − σ)`. Resolvendo para top 10% = 65% sai
σ ≈ 1,667 — e esse mesmo σ entrega top 1% = 25,5% e top 20% = 79,5%. Os três alvos
eram consistentes entre si; bastava acertar um.

**Um gerador por PDV, não um global.** O número de sorteios que um PDV consome
depende do seu porte (repertório maior = mais sorteios). Com um único gerador
global, mexer em `SIGMA_LOG` dessincroniza todo o fluxo seguinte e troca quem
recebe qual caso plantado — cada ajuste re-sorteia a base inteira e a
"sensibilidade" medida é ruído de realização. Foi exatamente o que aconteceu na
primeira rodada: um ajuste de σ de 1,225 para 1,175 derrubou o crescimento medido
de 12,5% para 3,1%. `random.Random(SEED + pdv_id)` por PDV resolve.

**Numa carteira Pareto, o agregado é hostage de um punhado de contas.** Com o 1%
maior carregando um quarto da receita, deixar o atrito *aleatório* sortear a 3ª
maior conta faz o crescimento agregado oscilar vários pontos entre realizações. O
topo ficou fora do sorteio de atrito — e não é só conveniência numérica:
distribuidor não perde conta-chave por acaso, perde por algo que se vê chegando.
Sangramento em conta grande continua existindo, mas plantado.

### Casos plantados de propósito

Sem plantar, o dashboard fica vazio. O gabarito fica em `dados/dim_pdv.csv`
(coluna `caso_plantado`), o que permite medir se o detector acha o que existe:

| Caso | Plantados | Encontrados |
|---|---|---|
| Sangramento silencioso — compra todo mês, perde ~80% da receita | 90 | 100% |
| Parada súbita — anos de regularidade e some | 110 | 100% |
| Perda de mix — volume parecido, larga metade das categorias | 80 | 98,8% |
| Sumiu e voltou — 6+ meses ausente, retorna no último mês | 45 | 100% |
| Queda em degrau — corte de portfólio num mês só | 70 | 95,7% |

Mais 1.260 PDVs de atrito natural, que povoam a fila de reativação.

> Esse recall mede que a implementação está correta, não desempenho em base real —
> onde a deterioração não vem rotulada nem foi desenhada para ser detectável. Quem
> apresenta recall de base sintética como acurácia de produção está vendendo o
> próprio gerador.

---

## A metodologia

Mês de referência = último mês fechado. Mês em andamento nunca entra: está sempre
incompleto e faz todo mundo parecer em queda.

**Três sinais**, todos comparando jan–jun do ano corrente contra jan–jun do ano
anterior — mesmo período, mesma sazonalidade, sobra só o que mudou no cliente:

1. **Receita** caiu mais de 30%
2. **Frequência** — 2 meses a menos com compra, contando só mês com **valor > 0**
3. **Mix** — categorias distintas do 2º trimestre caíram mais de 25%

As janelas são definidas de forma *relativa* ao mês de referência (`M-6..M-1` e
`M-18..M-13`). Com M = julho isso **é** jan–jun de cada ano, e o mesmo código serve
ao dashboard e a cada corte do backtest, sem reimplementar a regra duas vezes.

**Por que o valor > 0 importa:** a base tem 6.894 linhas de R$ 0,00 (bonificação,
troca, devolução que zerou) e 145 PDVs cuja contagem de meses ficaria inflada se a
régua aceitasse qualquer linha como compra. São justamente os que mais interessam —
alguns já pararam e seguem aparecendo no sistema com movimento de valor zero,
parecendo ativos.

**Faixas**, em ordem de precedência: `Parou` (3+ meses sem compra) → `Voltou`
(retornou após 3+ meses sumido) → `Sangrando` (2 sinais, ou um em nível extremo:
receita −50%, frequência −3 meses, mix −50%) → `Degradando` (1 sinal) → `Observar`
(receita entre −10% e −30%) → `Estável` (−10% a +10%) → `Crescendo` / `Novo`.

`Voltou` precede `Sangrando` por um motivo prático: quem acabou de voltar aparece
com queda enorme no semestre, e mandar o vendedor cobrar queda de um cliente que
retornou mês passado é o jeito mais rápido de perdê-lo de novo. São 236 PDVs nesta
base — sem a regra, todos entrariam na fila com o motivo errado.

**Duas listas de categoria por PDV**, numa régua de 12 meses mais rigorosa que a
dos sinais. Os sinais servem para classificar; as listas servem para o vendedor
ligar para o cliente:

- **Parou de comprar** — categoria comprada em **3+ meses distintos** nos 12 meses
  anteriores *e* zero nos últimos 6. Valor = o que gerou naqueles 12 meses, real,
  sem anualizar.
- **Continua comprando** — teve compra nos últimos 6 meses, com em quantos deles.

O filtro de 3+ meses distintos é o que faz a lista ser confiável: sem ele, uma
compra sazonal avulsa entra como categoria abandonada e o vendedor liga cobrando um
produto que o cliente nunca comprou de verdade. Basta acontecer duas ou três vezes
para a lista inteira perder credibilidade.

**Ranking** por receita de 12 meses, geral e dentro da tipologia, com a variação de
posição — é o que dá escala a "caiu 30%". Duas ressalvas ficam explícitas na página:
cada janela é ranqueada entre quem faturou *naquela* janela (denominadores
diferentes), e na cauda as posições estão coladas, então a queda só é destacada
para quem já era grande.

---

## Calibração por backtest

Os números da escada e do heatmap saem da própria base, não de fora. Corta-se num
mês do passado, medem-se os sinais usando **somente** dados até ali, e confere-se o
desfecho nos 12 meses seguintes (perdeu metade da receita, ou passou os últimos 6
meses sem comprar nada). Cinco cortes, 12.264 observações.

| Sinais no corte | Desfecho ruim | n |
|---|---|---|
| 0 | 15,6% | 8.154 |
| 1 | 28,2% | 2.436 |
| 2 | 37,3% | 797 |
| 3 | **69,1%** | 877 |

Com nenhum sinal, 15,6% pioram de qualquer forma — é o ruído de fundo da carteira, e
nenhuma régua vai abaixo dele. Com os três, 4,4× isso.

É também a razão de a base ter 5 anos e não 2: sem horizonte à frente do corte não
há o que conferir. Os cortes distam 6 meses, então as janelas se sobrepõem e as
observações não são independentes — serve para ordenar risco, não para cravar
probabilidade. A página diz isso.

**O heatmap ritmo × atraso é o achado mais útil.** Quem comprava quase todo mês e
está 3 a 5 meses sem comprar acaba mal em 96,7% dos casos; um cliente esporádico, no
mesmo atraso, em 33,9%. Mesmo atraso, quase 3× o risco. Cobrar os dois igual gasta a
viagem do vendedor no cliente errado — e é por isso que a régua de atraso não pode
ser um número só para toda a carteira.

Algumas células do heatmap são **impossíveis por definição**: quem comprou em 11 dos
12 meses não *pode* estar 3 meses sem comprar. Elas aparecem hachuradas, não como
0% — "não existe" e "existe e o risco é zero" são coisas diferentes.

---

## Notas de projeto

**Cor é dado, tinta é interface.** A interface não tem cor de destaque própria:
seleção é peso e marca de tinta. Assim nenhum cromo de UI compete com as cores de
status, e a única coisa colorida na tela é leitura de risco.

**Paleta validada, não estimada.** As cores passaram pelo validador dos seis checks
(banda de luminosidade, piso de croma, separação sob daltonismo, piso de visão
normal, contraste) nos dois temas. Duas correções vieram de lá: `Degradando` e
`Observar` estavam a ΔE 0,3 para protanopia — indistinguíveis — e a rampa de risco
não passava no piso de contraste nas pontas.

**A forma resolveu o que a cor não resolvia.** O validador insistia em reprovar a
pilha de 8 faixas por causa do cinza neutro do meio. O problema não era a matiz: era
a forma. Barras separadas e rotuladas eliminam pares adjacentes por construção — e
uma pilha de 8 categorias de status é ilegível de qualquer jeito.

**Status nunca pinta texto.** Rótulo de 10px em `--degradando` dá 2,75:1. O
quadradinho carrega a identidade, a tinta carrega a leitura, e o rótulo textual está
sempre presente: cor sozinha não informa nada nesta página.

**A série do detalhe tem 24 meses, não 18.** Com 18, a janela de comparação do ano
anterior cai parcialmente fora do gráfico e o usuário lê "−81%" sem conseguir ver
contra o quê.

---

## Arquivos

```
gerador_base.py         gera e valida a base sintética
termometro.py           metodologia: sinais, faixas, listas, ranking, backtest
build_dashboard.py      injeta o payload no template
dashboard_template.html a página (HTML/CSS/JS, sem dependências)
index.html              saída publicável, autocontida (servida pelo Pages)
dados/
  vendas.csv.gz         fato: pdv_id, mes, categoria_id, valor
  dim_pdv.csv           PDV + vendedor/supervisor + gabarito do caso plantado
  dim_categoria.csv     200 categorias inventadas por composição
  dim_mes.csv           calendário
  amostra_vendas.csv    3.000 primeiras linhas, legíveis sem descompactar
  resumo_geracao.json   validação da geração
  payload_dashboard.json  entrada da página
```

Semente fixa (`SEED = 20260808`): mesma semente, mesma base. Nomes de PDV, cidades e
categorias são inventados por composição e não correspondem a nada real.
