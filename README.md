# Ponto2 InkSugar — Banco de Horas

Flask + Postgres. Segunda geração do controle de ponto da InkSugar: o registro de
entrada/saída/almoço/home office e o cadastro da equipe continuam iguais, mas o
fechamento deixa de ser semanal e em dinheiro — agora é um **banco de horas**
contínuo, com adiantamentos abatidos do saldo.

Substitui o [`ponto-inksugar`](https://github.com/inksugar/ponto-inksugar) a
partir de setembro/2026. Roda em paralelo, em `/ponto2`, com banco e cadastro
próprios (sem migrar histórico do app antigo).

## Como funciona

**Para a equipe** — `/ponto2/ponto`
Igual ao app antigo: grade com a foto de cada uma, toca → registra entrada ou
saída (com almoço e, pra quem é híbrida, opção de home office).

**Meu extrato** — em vez de semanal, agora é **mensal**. Mostra, só em horas
(nunca em dinheiro): horas trabalhadas no mês, horas já recebidas (adiantamentos
do mês) e o saldo a receber (contínuo, pode ficar negativo). Abaixo, o dia a dia
do mês com entrada/saída/almoço/total, intercalado com as linhas de adiantamento
na data em que foram lançadas. Dá pra navegar por meses anteriores.

**Para a gestão** — `/ponto2/admin`
- Alertas de saída/entrada não registrada, igual ao app antigo.
- Cadastro: nome, cargo, híbrido, foto, situação (aparece no ponto / arquivada).
- **Registros da semana**: edição manual de entrada/saída/almoço, sem valores.
- **Adiantamentos**: tela pra lançar o adiantamento semanal — mostra o saldo de
  cada pessoa, sugere abater 40h de quem tem saldo (ou só o disponível pra quem
  tem menos), e permite editar ou excluir qualquer lançamento.

## Instalação

### 1. Banco — Neon
Projeto novo, separado do banco do `ponto-inksugar`. Região São Paulo → copiar a
connection string.

### 2. Deploy — VPS (produção)
Ver `CLAUDE.md` (não versionado) pra detalhes de infraestrutura.

### 3. Rodar local
```
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."
python app.py
```
Sobe em `http://localhost:5001/ponto2/ponto` (a rota já nasce com o prefixo
`/ponto2`, pra bater com o path da VPS).
