import base64
import calendar
import os
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response

TZ = ZoneInfo("America/Sao_Paulo")
DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
         "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
MESES_ABREV = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
ALMOCO_PADRAO = 30
JORNADA_LIMITE = 420  # 7h em minutos

# App mora em /ponto2 na mesma VPS do ponto-inksugar original — nginx encaminha
# o caminho inteiro (sem tirar o prefixo), então toda rota e todo link do
# template já nasce com "/ponto2" na frente. Não usar url_for cru sem isso.
app = Flask(__name__, static_url_path="/ponto2/static")
app.secret_key = os.environ.get("SECRET_KEY", "troque-isso")
app.permanent_session_lifetime = timedelta(days=30)


@app.after_request
def sem_cache(resp):
    # páginas dinâmicas nunca devem ficar em cache no navegador — importante
    # sobretudo logo depois de uma virada de app, pra ninguém ver tela velha.
    # /static (fotos, ícones, manifest, sw.js) continua cacheável normalmente.
    if not request.path.startswith("/ponto2/static"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp

DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_SENHA = os.environ.get("ADMIN_SENHA", "inksugar")
CRON_TOKEN = os.environ.get("CRON_TOKEN", "")
WHATS_PHONE = os.environ.get("WHATS_PHONE", "")
WHATS_APIKEY = os.environ.get("WHATS_APIKEY", "")


@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS funcionarios (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                cargo TEXT NOT NULL DEFAULT '',
                hibrido BOOLEAN NOT NULL DEFAULT FALSE,
                foto TEXT,
                aparece_no_ponto BOOLEAN NOT NULL DEFAULT TRUE,
                arquivado BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS pontos (
                id SERIAL PRIMARY KEY,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
                dia DATE NOT NULL,
                entrada TIMESTAMP,
                saida TIMESTAMP,
                almoco_min INTEGER NOT NULL DEFAULT 0,
                minutos INTEGER,
                local TEXT NOT NULL DEFAULT 'interno'
            );
            CREATE TABLE IF NOT EXISTS adiantamentos (
                id SERIAL PRIMARY KEY,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
                data DATE NOT NULL,
                minutos INTEGER NOT NULL DEFAULT 2400,
                obs TEXT DEFAULT '',
                criado_em TIMESTAMP NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS taxas (
                id SERIAL PRIMARY KEY,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
                valor_hora NUMERIC(10,2) NOT NULL,
                valor_hora_home NUMERIC(10,2) NOT NULL DEFAULT 0,
                vigente_desde DATE NOT NULL,
                criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (funcionario_id, vigente_desde)
            );
            CREATE INDEX IF NOT EXISTS idx_taxas_funcionario ON taxas (funcionario_id, vigente_desde);
            CREATE INDEX IF NOT EXISTS idx_pontos_dia ON pontos (dia);
            CREATE INDEX IF NOT EXISTS idx_pontos_funcionario_dia ON pontos (funcionario_id, dia);
            CREATE INDEX IF NOT EXISTS idx_adiantamentos_funcionario_data ON adiantamentos (funcionario_id, data);
            ALTER TABLE taxas ADD COLUMN IF NOT EXISTS valor_hora_home NUMERIC(10,2) NOT NULL DEFAULT 0;
            ALTER TABLE funcionarios ADD COLUMN IF NOT EXISTS entrada_padrao TIME;
            ALTER TABLE funcionarios ADD COLUMN IF NOT EXISTS saida_padrao TIME;
            ALTER TABLE funcionarios ADD COLUMN IF NOT EXISTS almoco_padrao_min INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE funcionarios ADD COLUMN IF NOT EXISTS adiantamento_fixo NUMERIC(10,2) NOT NULL DEFAULT 0;
            ALTER TABLE adiantamentos ADD COLUMN IF NOT EXISTS mes_ref DATE;
            ALTER TABLE adiantamentos ADD COLUMN IF NOT EXISTS valor_pago NUMERIC(10,2);
            ALTER TABLE adiantamentos ALTER COLUMN minutos DROP NOT NULL;
            ALTER TABLE adiantamentos ALTER COLUMN minutos DROP DEFAULT;
        """)


def agora():
    return datetime.now(TZ).replace(tzinfo=None)


def hoje():
    return datetime.now(TZ).date()


def hm(minutos):
    if minutos is None:
        return "—"
    minutos = int(minutos)
    return f"{minutos // 60}h{minutos % 60:02d}"


def hm_sinal(minutos):
    """Como hm(), mas mostra o sinal — usado pro saldo, que pode ficar negativo."""
    if minutos is None:
        return "—"
    minutos = int(minutos)
    sinal = "−" if minutos < 0 else ""
    minutos = abs(minutos)
    return f"{sinal}{minutos // 60}h{minutos % 60:02d}"


def semana_de(d):
    ini = d - timedelta(days=d.weekday())
    return ini, ini + timedelta(days=6)


def mes_de(ano, mes):
    ini = date(ano, mes, 1)
    fim = date(ano, mes, calendar.monthrange(ano, mes)[1])
    return ini, fim


def iniciais(nome):
    p = [x for x in nome.split() if x]
    if not p:
        return "?"
    return (p[0][0] + (p[-1][0] if len(p) > 1 else "")).upper()


def dinheiro(txt):
    txt = (txt or "0").strip().replace("R$", "").replace(" ", "")
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    try:
        return round(float(txt), 2)
    except ValueError:
        return 0.0


def brl(v):
    if v is None:
        return "—"
    v = round(float(v), 2)
    if v == 0:
        v = 0.0  # evita "-0,00" quando um resto de ponto flutuante arredonda pra zero negativo
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def hora_ou_none(txt):
    txt = (txt or "").strip()
    if not txt:
        return None
    try:
        return datetime.strptime(txt, "%H:%M").time()
    except ValueError:
        return None


def mes_ref_ou_none(txt):
    """Campo <input type=month> manda 'AAAA-MM' — guarda como o dia 1 desse
    mês, ou None se ela deixar em branco (aí conta pela data real do
    lançamento, não por uma referência escolhida à mão)."""
    txt = (txt or "").strip()
    if not txt:
        return None
    try:
        ano_s, mes_s = txt.split("-")
        return date(int(ano_s), int(mes_s), 1)
    except (ValueError, AttributeError):
        return None


app.jinja_env.globals.update(hm=hm, hm_sinal=hm_sinal, iniciais=iniciais, brl=brl, MESES_ABREV=MESES_ABREV)


def calcula(entrada, saida, almoco):
    bruto = int((saida - entrada).total_seconds() // 60)
    trabalhado = max(0, bruto - almoco)
    ajustado = False
    if trabalhado >= JORNADA_LIMITE and almoco < ALMOCO_PADRAO:
        almoco = ALMOCO_PADRAO
        trabalhado = max(0, bruto - almoco)
        ajustado = True
    return almoco, trabalhado, ajustado


def saldo_ate(cur, fid, data_corte):
    """Saldo do banco de horas até uma data (inclusive): trabalhado acumulado
    menos adiantamentos já lançados. Contínuo desde o início do cadastro,
    pode ficar negativo."""
    cur.execute(
        "SELECT COALESCE(SUM(minutos),0) FROM pontos WHERE funcionario_id=%s AND dia<=%s AND minutos IS NOT NULL",
        (fid, data_corte),
    )
    trabalhado = int(cur.fetchone()[0])
    cur.execute(
        "SELECT COALESCE(SUM(minutos),0) FROM adiantamentos WHERE funcionario_id=%s AND data<=%s",
        (fid, data_corte),
    )
    abatido = int(cur.fetchone()[0])
    return trabalhado - abatido


def taxas_historico(cur, fid):
    """Todo o histórico de taxas da pessoa, mais recente primeiro — busca uma
    vez só (a tabela é pequena, poucas linhas por pessoa) pra depois resolver
    'qual taxa valia nessa data' inteiramente em memória, sem bater no banco
    de novo pra cada dia trabalhado. Isso importa: um mês tem ~20-30 dias e o
    histórico completo pode ter centenas de registros de pontos — fazer uma
    query por linha (como era antes) deixava a página levando 10+ segundos."""
    cur.execute("""
        SELECT valor_hora, valor_hora_home, vigente_desde FROM taxas
        WHERE funcionario_id=%s ORDER BY vigente_desde DESC
    """, (fid,))
    return [(float(r[0]), float(r[1] or 0), r[2]) for r in cur.fetchall()]


def taxa_em(historico, data_ref):
    """Valor/hora (interno, home) vigente numa data, procurando em memória no
    histórico já carregado — a mais recente cujo vigente_desde seja <= data_ref.
    Mudar o valor nunca reescreve o passado — cada alteração vale só a partir
    da data escolhida."""
    for vh, vhh, vigente_desde in historico:
        if vigente_desde <= data_ref:
            return vh, vhh
    return 0.0, 0.0


def valor_trabalhado_ate(cur, fid, data_corte, historico=None):
    """Soma, em R$, do que foi trabalhado até uma data — cada dia valorizado
    pela taxa vigente naquele dia (interno ou home conforme o local)."""
    if historico is None:
        historico = taxas_historico(cur, fid)
    cur.execute(
        "SELECT dia, minutos, local FROM pontos WHERE funcionario_id=%s AND dia<=%s AND minutos IS NOT NULL",
        (fid, data_corte),
    )
    total = 0.0
    for dia, minutos, local in cur.fetchall():
        vh, vhh = taxa_em(historico, dia)
        total += (minutos / 60) * (vhh if local == "home" else vh)
    return total


def valor_pago_ate(cur, fid, data_corte, historico=None):
    """Soma, em R$, do que já foi pago via adiantamento até uma data.
    Lançamentos novos guardam o valor direto; os antigos (em minutos, de
    antes dessa mudança) são convertidos pela taxa vigente na data deles."""
    cur.execute(
        "SELECT data, minutos, valor_pago FROM adiantamentos WHERE funcionario_id=%s AND data<=%s",
        (fid, data_corte),
    )
    total = 0.0
    linhas = cur.fetchall()
    if any(v is None and m for _, m, v in linhas):
        if historico is None:
            historico = taxas_historico(cur, fid)
    for data_l, minutos, valor_pago in linhas:
        if valor_pago is not None:
            total += float(valor_pago)
        elif minutos:
            vh, _ = taxa_em(historico, data_l)
            total += (minutos / 60) * vh
    return total


def saldo_valor_ate(cur, fid, data_corte, historico=None):
    if historico is None:
        historico = taxas_historico(cur, fid)
    return round(valor_trabalhado_ate(cur, fid, data_corte, historico)
                 - valor_pago_ate(cur, fid, data_corte, historico), 2)


def saldos_em_lote(cur, ids, data_corte):
    """Igual a saldo_valor_ate, mas pra várias pessoas de uma vez só — 3
    consultas no total em vez de 3 por pessoa. Usado em /admin/adiantamentos,
    que senão fica lento com a equipe inteira (cada pessoa a mais somava
    mais idas e vindas ao banco). Devolve {funcionario_id: (saldo, historico)}."""
    if not ids:
        return {}
    cur.execute("""
        SELECT funcionario_id, valor_hora, valor_hora_home, vigente_desde FROM taxas
        WHERE funcionario_id = ANY(%s) ORDER BY funcionario_id, vigente_desde DESC
    """, (ids,))
    historicos = {}
    for fid, vh, vhh, vigente_desde in cur.fetchall():
        historicos.setdefault(fid, []).append((float(vh), float(vhh or 0), vigente_desde))

    cur.execute("""
        SELECT funcionario_id, dia, minutos, local FROM pontos
        WHERE funcionario_id = ANY(%s) AND dia<=%s AND minutos IS NOT NULL
    """, (ids, data_corte))
    trabalhado = {fid: 0.0 for fid in ids}
    for fid, dia, minutos, local in cur.fetchall():
        vh, vhh = taxa_em(historicos.get(fid, []), dia)
        trabalhado[fid] += (minutos / 60) * (vhh if local == "home" else vh)

    cur.execute("""
        SELECT funcionario_id, data, minutos, valor_pago FROM adiantamentos
        WHERE funcionario_id = ANY(%s) AND data<=%s
    """, (ids, data_corte))
    pago = {fid: 0.0 for fid in ids}
    for fid, data_l, minutos, valor_pago in cur.fetchall():
        if valor_pago is not None:
            pago[fid] += float(valor_pago)
        elif minutos:
            vh, _ = taxa_em(historicos.get(fid, []), data_l)
            pago[fid] += (minutos / 60) * vh

    return {fid: (round(trabalhado[fid] - pago[fid], 2), historicos.get(fid, [])) for fid in ids}


# ---------------- Telas da equipe ----------------

@app.route("/ponto2/")
def home():
    return redirect(url_for("ponto"))


@app.route("/ponto2/ping")
def ping():
    return "ok", 200, {"Content-Type": "text/plain"}


@app.route("/ponto2/sw.js")
def sw():
    return app.send_static_file("sw.js"), 200, {"Content-Type": "application/javascript"}


@app.route("/ponto2/manifest.json")
def manifest():
    return app.send_static_file("manifest.json"), 200, {"Content-Type": "application/manifest+json"}


@app.route("/ponto2/manifest/<int:fid>.json")
def manifest_pessoa(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s", (fid,))
        f = cur.fetchone()
    if not f:
        return redirect(url_for("manifest"))
    icone = url_for("icone_pessoa", fid=fid) if f["foto"] else "/ponto2/static/icon-512.png"
    dados = {
        "name": f"Banco de Horas — {f['nome']}",
        "short_name": f["nome"].split()[0],
        "start_url": url_for("pessoa", fid=fid),
        "scope": "/ponto2/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0A0A0B",
        "theme_color": "#0A0A0B",
        "lang": "pt-BR",
        "icons": [
            {"src": icone, "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": icone, "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return jsonify(dados), 200, {"Content-Type": "application/manifest+json"}


@app.route("/ponto2/icone/<int:fid>.png")
def icone_pessoa(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT foto FROM funcionarios WHERE id=%s", (fid,))
        f = cur.fetchone()
    if not f or not f["foto"] or "," not in f["foto"]:
        return redirect("/ponto2/static/icon-512.png")
    try:
        bruto = base64.b64decode(f["foto"].split(",", 1)[1])
    except Exception:
        return redirect("/ponto2/static/icon-512.png")
    return Response(bruto, mimetype="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/ponto2/ponto")
def ponto():
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE aparece_no_ponto AND NOT arquivado ORDER BY nome")
        equipe = cur.fetchall()
    return render_template("ponto.html", equipe=equipe)


def registro_aberto(cur, fid):
    cur.execute(
        "SELECT * FROM pontos WHERE funcionario_id=%s AND saida IS NULL ORDER BY entrada DESC LIMIT 1",
        (fid,),
    )
    return cur.fetchone()


@app.route("/ponto2/ponto/<int:fid>")
def pessoa(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s AND NOT arquivado", (fid,))
        f = cur.fetchone()
        if not f:
            return redirect(url_for("ponto"))
        aberto = registro_aberto(cur, fid)
        cur.execute(
            "SELECT * FROM pontos WHERE funcionario_id=%s AND dia=%s AND saida IS NOT NULL ORDER BY saida DESC LIMIT 1",
            (fid, hoje()),
        )
        fechado = cur.fetchone()
    return render_template("pessoa.html", f=f, aberto=aberto, fechado=fechado,
                           feito=request.args.get("feito"),
                           ajustado=request.args.get("ajustado"))


@app.route("/ponto2/ponto/<int:fid>/mes")
def mes_pessoa(fid):
    hj = hoje()
    try:
        ano = int(request.args.get("ano") or hj.year)
        mes = int(request.args.get("mes") or hj.month)
        date(ano, mes, 1)
    except (TypeError, ValueError):
        ano, mes = hj.year, hj.month
    ini, fim = mes_de(ano, mes)

    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s AND NOT arquivado", (fid,))
        f = cur.fetchone()
        if not f:
            return redirect(url_for("ponto"))

        eventos, trabalhadas, valor_trabalhado, valor_pago = extrato_do_mes(cur, fid, ini, fim)

        # "Pago no mês" já veio certo do extrato_do_mes: cada adiantamento
        # conta pro mês do campo "mês de referência" quando ela escolhe um
        # (ex.: adiantamento fixo pago dia 7, referente ao mês anterior) —
        # sem mês de referência, conta pela data real do lançamento. Por
        # isso "falta no mês" agora é só a conta direta, sem precisar de
        # FIFO entre meses.
        falta_mes = round(valor_trabalhado - valor_pago, 2)
        # saldo "como estava no fim desse mês" (não o saldo de hoje) — senão,
        # ao olhar um mês passado, um trabalho ainda não pago de um mês
        # futuro aparecia ali, confundindo (a pendência de setembro
        # aparecendo na página de agosto).
        saldo = saldo_valor_ate(cur, fid, min(fim, hj))

        # dias do mês inteiro (com ou sem registro) pra edição em quinzenas,
        # igual /admin/registros — precisa mostrar os dias em branco também
        # pro botão "usar horário padrão" ter onde preencher.
        linhas_completas, _ = periodo_do_funcionario(cur, fid, ini, fim)

    ant_ano, ant_mes = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    prox_ano, prox_mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    atual = (ano == hj.year and mes == hj.month)
    nome_mes = f"{MESES[mes - 1]} de {ano}"

    # de volta ao formato em quinzenas (1-15 / 16-fim) lado a lado, com todo
    # dia do mês (não só os que têm registro) — os pagamentos saem da tabela
    # de dias e viram uma lista à parte, embaixo
    primeira = [l for l in linhas_completas if l["data"].day <= 15]
    segunda = [l for l in linhas_completas if l["data"].day > 15]
    pagamentos = [e for e in eventos if e["tipo"] == "adiantamento"]

    return render_template("mes.html", f=f, primeira=primeira, segunda=segunda, pagamentos=pagamentos,
                           n_trabalho=len(linhas_completas), ini=ini, fim=fim, nome_mes=nome_mes,
                           trabalhadas=trabalhadas, valor_trabalhado=valor_trabalhado,
                           valor_pago=valor_pago, falta_mes=falta_mes, saldo=saldo,
                           ano=ano, mes=mes, atual=atual, is_admin=bool(session.get("admin")),
                           ant_ano=ant_ano, ant_mes=ant_mes, prox_ano=prox_ano, prox_mes=prox_mes)


@app.route("/ponto2/ponto/<int:fid>/entrada", methods=["POST"])
def bater_entrada(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s AND NOT arquivado", (fid,))
        f = cur.fetchone()
        if not f:
            return redirect(url_for("ponto"))
        if registro_aberto(cur, fid):
            return redirect(url_for("pessoa", fid=fid))
        n = agora()
        cur.execute(
            "INSERT INTO pontos (funcionario_id, dia, entrada) VALUES (%s,%s,%s)",
            (fid, n.date(), n),
        )
    return redirect(url_for("pessoa", fid=fid, feito="entrada"))


@app.route("/ponto2/ponto/<int:fid>/saida", methods=["POST"])
def bater_saida(fid):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE id=%s AND NOT arquivado", (fid,))
        f = cur.fetchone()
        if not f:
            return redirect(url_for("ponto"))
        aberto = registro_aberto(cur, fid)
        if not aberto:
            return redirect(url_for("pessoa", fid=fid))
        n = agora()
        try:
            almoco = max(0, min(240, int(request.form.get("almoco") or 0)))
        except ValueError:
            almoco = ALMOCO_PADRAO
        local = "home" if (f["hibrido"] and request.form.get("home")) else "interno"
        almoco, minutos, ajustado = calcula(aberto["entrada"], n, almoco)
        cur.execute(
            "UPDATE pontos SET saida=%s, almoco_min=%s, minutos=%s, local=%s WHERE id=%s",
            (n, almoco, minutos, local, aberto["id"]),
        )
    return redirect(url_for("pessoa", fid=fid, feito="saida",
                            ajustado=1 if ajustado else None))


# ---------------- Admin ----------------

def admin_only(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


@app.route("/ponto2/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("senha") == ADMIN_SENHA:
            session.permanent = True
            session["admin"] = True
            return redirect(url_for("admin"))
        return render_template("login.html", erro="Senha incorreta.")
    return render_template("login.html")


@app.route("/ponto2/admin/sair")
def logout():
    session.clear()
    return redirect(url_for("ponto"))


def pendencias(cur):
    cur.execute("""
        SELECT p.id, p.dia, f.nome
        FROM pontos p JOIN funcionarios f ON f.id = p.funcionario_id
        WHERE p.saida IS NULL AND p.dia < %s AND NOT f.arquivado
        ORDER BY p.dia
    """, (hoje(),))
    abertos = cur.fetchall()
    cur.execute("""
        SELECT f.nome FROM funcionarios f
        WHERE f.aparece_no_ponto AND NOT f.arquivado AND NOT EXISTS (
            SELECT 1 FROM pontos p WHERE p.funcionario_id = f.id AND p.dia = %s
        ) ORDER BY f.nome
    """, (hoje(),))
    sem_entrada = [r["nome"] for r in cur.fetchall()]
    return abertos, sem_entrada


@app.route("/ponto2/admin")
@admin_only
def admin():
    hj = hoje()
    ini, fim = semana_de(hj)
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE NOT arquivado ORDER BY aparece_no_ponto DESC, nome")
        equipe = []
        for f in cur.fetchall():
            d = dict(f)
            cur.execute("SELECT * FROM taxas WHERE funcionario_id=%s ORDER BY vigente_desde DESC", (f["id"],))
            d["taxas"] = cur.fetchall()
            d["taxa_atual"] = next((t for t in d["taxas"] if t["vigente_desde"] <= hj), None)
            equipe.append(d)
        cur.execute("SELECT * FROM funcionarios WHERE arquivado ORDER BY nome")
        arquivados = cur.fetchall()
        abertos, sem_entrada = pendencias(cur)
    return render_template("admin.html", equipe=equipe, arquivados=arquivados, ini=ini, fim=fim,
                           abertos=abertos, sem_entrada=sem_entrada,
                           dia_semana=hj.weekday(), hoje=hj)


@app.route("/ponto2/admin/funcionario", methods=["POST"])
@admin_only
def salvar_funcionario():
    fid = request.form.get("id")
    nome = (request.form.get("nome") or "").strip()
    cargo = (request.form.get("cargo") or "").strip()
    hibrido = bool(request.form.get("hibrido"))
    aparece_no_ponto = bool(request.form.get("aparece_no_ponto"))
    arquivado = bool(request.form.get("arquivado"))
    foto = request.form.get("foto") or None
    entrada_padrao = hora_ou_none(request.form.get("entrada_padrao"))
    saida_padrao = hora_ou_none(request.form.get("saida_padrao"))
    try:
        almoco_padrao_min = max(0, min(240, int(request.form.get("almoco_padrao_min") or 0)))
    except ValueError:
        almoco_padrao_min = 0
    adiantamento_fixo = dinheiro(request.form.get("adiantamento_fixo"))
    with db() as conn, conn.cursor() as cur:
        if fid:
            if foto:
                cur.execute("""UPDATE funcionarios SET nome=%s,cargo=%s,hibrido=%s,
                               aparece_no_ponto=%s,arquivado=%s,foto=%s,
                               entrada_padrao=%s,saida_padrao=%s,almoco_padrao_min=%s,
                               adiantamento_fixo=%s WHERE id=%s""",
                            (nome, cargo, hibrido, aparece_no_ponto, arquivado, foto,
                             entrada_padrao, saida_padrao, almoco_padrao_min, adiantamento_fixo, fid))
            else:
                cur.execute("""UPDATE funcionarios SET nome=%s,cargo=%s,hibrido=%s,
                               aparece_no_ponto=%s,arquivado=%s,
                               entrada_padrao=%s,saida_padrao=%s,almoco_padrao_min=%s,
                               adiantamento_fixo=%s WHERE id=%s""",
                            (nome, cargo, hibrido, aparece_no_ponto, arquivado,
                             entrada_padrao, saida_padrao, almoco_padrao_min, adiantamento_fixo, fid))
        else:
            cur.execute("""INSERT INTO funcionarios (nome,cargo,hibrido,foto,
                           aparece_no_ponto,arquivado,entrada_padrao,saida_padrao,almoco_padrao_min,
                           adiantamento_fixo)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (nome, cargo, hibrido, foto, aparece_no_ponto, arquivado,
                         entrada_padrao, saida_padrao, almoco_padrao_min, adiantamento_fixo))
            fid = cur.fetchone()[0]
    return redirect(url_for("admin") + f"#f{fid}")


@app.route("/ponto2/admin/funcionario/<int:fid>/excluir", methods=["POST"])
@admin_only
def excluir_funcionario(fid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM funcionarios WHERE id=%s", (fid,))
    return redirect(url_for("admin"))


@app.route("/ponto2/admin/funcionario/<int:fid>/desarquivar", methods=["POST"])
@admin_only
def desarquivar_funcionario(fid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE funcionarios SET arquivado=FALSE WHERE id=%s", (fid,))
    return redirect(url_for("admin") + f"#f{fid}")


# --------- Valor da hora (histórico, vale a partir da data escolhida) ---------

@app.route("/ponto2/admin/funcionario/<int:fid>/taxa", methods=["POST"])
@admin_only
def salvar_taxa(fid):
    valor = dinheiro(request.form.get("valor_hora"))
    valor_home = dinheiro(request.form.get("valor_hora_home"))
    try:
        vigente_desde = date.fromisoformat(request.form.get("vigente_desde") or "")
    except ValueError:
        vigente_desde = hoje()
    if valor > 0:
        with db() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO taxas (funcionario_id, valor_hora, valor_hora_home, vigente_desde) VALUES (%s,%s,%s,%s)
                ON CONFLICT (funcionario_id, vigente_desde)
                DO UPDATE SET valor_hora=EXCLUDED.valor_hora, valor_hora_home=EXCLUDED.valor_hora_home
            """, (fid, valor, valor_home, vigente_desde))
    return redirect(url_for("admin") + f"#f{fid}")


@app.route("/ponto2/admin/taxa/<int:tid>/excluir", methods=["POST"])
@admin_only
def excluir_taxa(tid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT funcionario_id FROM taxas WHERE id=%s", (tid,))
        r = cur.fetchone()
        fid = r[0] if r else None
        cur.execute("DELETE FROM taxas WHERE id=%s", (tid,))
    return redirect(url_for("admin") + (f"#f{fid}" if fid else ""))


# --------- Registros (correção manual de entrada/saída) ---------

def periodo_do_funcionario(cur, fid, ini, fim):
    """Todos os dias de ini a fim (inclusive), com o(s) registro(s) daquele dia
    ou None quando não bateu ponto — usado tanto pra grade semanal quanto
    mensal de edição manual."""
    cur.execute("""
        SELECT * FROM pontos WHERE funcionario_id=%s AND dia BETWEEN %s AND %s
        ORDER BY dia, entrada
    """, (fid, ini, fim))
    regs = {}
    for r in cur.fetchall():
        regs.setdefault(r["dia"], []).append(r)
    linhas, total = [], 0
    d = ini
    while d <= fim:
        for r in regs.get(d, [None]):
            if r and r["minutos"]:
                total += r["minutos"]
            linhas.append({"dia": DIAS[d.weekday()], "data": d, "reg": r, "idx": len(linhas)})
        d += timedelta(days=1)
    return linhas, total


@app.route("/ponto2/admin/registros")
@admin_only
def registros():
    hj = hoje()
    try:
        ano = int(request.args.get("ano") or hj.year)
        mes = int(request.args.get("mes") or hj.month)
        date(ano, mes, 1)
    except (TypeError, ValueError):
        ano, mes = hj.year, hj.month
    ini, fim = mes_de(ano, mes)
    fid = request.args.get("f")
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        if fid:
            cur.execute("SELECT * FROM funcionarios WHERE id=%s", (fid,))
            equipe = cur.fetchall()
        else:
            cur.execute("SELECT * FROM funcionarios WHERE NOT arquivado ORDER BY nome")
            equipe = cur.fetchall()
        blocos = []
        for f in equipe:
            linhas, total = periodo_do_funcionario(cur, f["id"], ini, fim)
            # duas colunas lado a lado (quinzenas), pra não ficar um telão vertical
            primeira = [l for l in linhas if l["data"].day <= 15]
            segunda = [l for l in linhas if l["data"].day > 15]
            blocos.append({"f": f, "primeira": primeira, "segunda": segunda,
                           "total": total, "n": len(linhas)})
    ant_ano, ant_mes = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    prox_ano, prox_mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    atual = (ano == hj.year and mes == hj.month)
    nome_mes = f"{MESES[mes - 1]} de {ano}"
    return render_template("registros.html", blocos=blocos, ini=ini, fim=fim, nome_mes=nome_mes,
                           ano=ano, mes=mes, atual=atual,
                           ant_ano=ant_ano, ant_mes=ant_mes, prox_ano=prox_ano, prox_mes=prox_mes,
                           filtro=fid)


@app.route("/ponto2/admin/registros/salvar", methods=["POST"])
@admin_only
def salvar_registros_pessoa():
    fid = request.form["funcionario_id"]
    hj = hoje()
    try:
        ano = int(request.form.get("ano") or hj.year)
        mes = int(request.form.get("mes") or hj.month)
    except ValueError:
        ano, mes = hj.year, hj.month
    try:
        n = max(0, min(60, int(request.form.get("n") or 0)))
    except ValueError:
        n = 0
    with db() as conn, conn.cursor() as cur:
        for i in range(n):
            try:
                d = date.fromisoformat(request.form.get(f"dia_{i}") or "")
            except ValueError:
                continue
            rid = (request.form.get(f"id_{i}") or "").strip()
            e = (request.form.get(f"entrada_{i}") or "").strip()
            s = (request.form.get(f"saida_{i}") or "").strip()
            try:
                almoco = max(0, min(240, int(request.form.get(f"almoco_{i}") or 0)))
            except ValueError:
                almoco = 0
            local = "home" if (request.form.get(f"local_{i}") == "home") else "interno"
            entrada = datetime.combine(d, datetime.strptime(e, "%H:%M").time()) if e else None
            saida = datetime.combine(d, datetime.strptime(s, "%H:%M").time()) if s else None
            if entrada and saida and saida < entrada:
                saida += timedelta(days=1)
            minutos = max(0, int((saida - entrada).total_seconds() // 60) - almoco) if entrada and saida else None
            if rid:
                if not entrada and not saida:
                    cur.execute("DELETE FROM pontos WHERE id=%s", (rid,))
                else:
                    cur.execute("""UPDATE pontos SET dia=%s,entrada=%s,saida=%s,almoco_min=%s,minutos=%s,local=%s
                                   WHERE id=%s""", (d, entrada, saida, almoco, minutos, local, rid))
            elif entrada or saida:
                cur.execute("""INSERT INTO pontos (funcionario_id,dia,entrada,saida,almoco_min,minutos,local)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                            (fid, d, entrada, saida, almoco, minutos, local))
    return redirect(request.form.get("voltar") or url_for("registros", ano=ano, mes=mes))


# --------- Banco de horas: adiantamentos ---------

def extrato_do_mes(cur, fid, ini, fim):
    """Trabalho + adiantamentos de um funcionário num período, intercalados
    cronologicamente, cada evento já com o valor em R$ (trabalho valorizado
    pela taxa vigente no dia; adiantamento com o valor pago direto, ou
    convertido pela taxa se for um lançamento antigo em horas) — mesma
    lógica usada no extrato mensal dela (/ponto/<id>/mes) e reaproveitada
    aqui pro extrato por pessoa da tela de adiantamentos."""
    cur.execute(
        "SELECT * FROM pontos WHERE funcionario_id=%s AND dia BETWEEN %s AND %s ORDER BY dia, entrada",
        (fid, ini, fim),
    )
    pontos_periodo = cur.fetchall()
    # conta pelo mês de referência quando ela escolheu um (pagamento de
    # adiantamento fixo feito dia 7 pode ser referente ao mês anterior, por
    # exemplo) — sem mês de referência escolhido, conta pela data real do
    # lançamento, como antes.
    cur.execute(
        "SELECT * FROM adiantamentos WHERE funcionario_id=%s AND COALESCE(mes_ref, data) BETWEEN %s AND %s ORDER BY data",
        (fid, ini, fim),
    )
    adiant_periodo = cur.fetchall()
    historico = taxas_historico(cur, fid)

    trabalhadas = sum(p["minutos"] or 0 for p in pontos_periodo)
    valor_trabalhado = 0.0
    eventos = []
    for p in pontos_periodo:
        valor_dia = 0.0
        if p["minutos"]:
            vh, vhh = taxa_em(historico, p["dia"])
            valor_dia = round((p["minutos"] / 60) * (vhh if p["local"] == "home" else vh), 2)
            valor_trabalhado += valor_dia
        eventos.append({"tipo": "trabalho", "data": p["dia"], "dia_semana": DIAS[p["dia"].weekday()],
                        "reg": p, "valor": valor_dia})

    valor_pago = 0.0
    for a in adiant_periodo:
        if a["valor_pago"] is not None:
            valor_a = float(a["valor_pago"])
        else:
            vh, _ = taxa_em(historico, a["data"])
            valor_a = round(((a["minutos"] or 0) / 60) * vh, 2)
        valor_pago += valor_a
        eventos.append({"tipo": "adiantamento", "data": a["data"], "dia_semana": DIAS[a["data"].weekday()],
                        "reg": a, "valor": valor_a})

    eventos.sort(key=lambda e: (e["data"], 0 if e["tipo"] == "trabalho" else 1))
    idx = 0
    for e in eventos:
        if e["tipo"] == "trabalho":
            e["idx"] = idx
            idx += 1
    return eventos, trabalhadas, round(valor_trabalhado, 2), round(valor_pago, 2)


@app.route("/ponto2/admin/adiantamentos")
@admin_only
def adiantamentos():
    hj = hoje()
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE NOT arquivado ORDER BY nome")
        equipe = cur.fetchall()
        saldos = saldos_em_lote(cur, [f["id"] for f in equipe], hj)
        linhas = []
        for f in equipe:
            saldo, historico = saldos[f["id"]]
            fixo = float(f["adiantamento_fixo"] or 0)
            if fixo > 0:
                sugestao = fixo
            else:
                vh, vhh = taxa_em(historico, hj)
                teto_40h = round(40 * vh, 2)
                sugestao = min(saldo, teto_40h) if saldo > 0 else 0
            linhas.append({"f": f, "saldo": saldo, "sugestao": sugestao})
    return render_template("adiantamentos.html", linhas=linhas, hoje=hj)


@app.route("/ponto2/admin/adiantamentos/lancar", methods=["POST"])
@admin_only
def lancar_adiantamentos():
    try:
        data_lote = date.fromisoformat(request.form.get("data") or "")
    except ValueError:
        data_lote = hoje()
    mes_ref_lote = mes_ref_ou_none(request.form.get("mes_ref"))
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM funcionarios WHERE NOT arquivado")
        for (fid,) in cur.fetchall():
            campo = (request.form.get(f"valor_{fid}") or "").strip()
            if not campo:
                continue
            valor_v = dinheiro(campo)
            if valor_v <= 0:
                continue
            cur.execute(
                "INSERT INTO adiantamentos (funcionario_id, data, valor_pago, mes_ref) VALUES (%s,%s,%s,%s)",
                (fid, data_lote, valor_v, mes_ref_lote),
            )
    return redirect(url_for("adiantamentos"))


@app.route("/ponto2/admin/adiantamento/<int:aid>", methods=["POST"])
@admin_only
def salvar_adiantamento(aid):
    voltar = request.form.get("voltar")
    try:
        data_v = date.fromisoformat(request.form["data"])
    except (KeyError, ValueError):
        return redirect(voltar or url_for("adiantamentos"))
    valor_v = dinheiro(request.form.get("valor"))
    obs = (request.form.get("obs") or "").strip()[:200]
    mes_ref_v = mes_ref_ou_none(request.form.get("mes_ref"))
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE adiantamentos SET data=%s, valor_pago=%s, minutos=NULL, obs=%s, mes_ref=%s WHERE id=%s",
                    (data_v, valor_v, obs, mes_ref_v, aid))
    return redirect(voltar or url_for("adiantamentos"))


@app.route("/ponto2/admin/adiantamento/<int:aid>/excluir", methods=["POST"])
@admin_only
def excluir_adiantamento(aid):
    voltar = request.form.get("voltar")
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM adiantamentos WHERE id=%s", (aid,))
    return redirect(voltar or url_for("adiantamentos"))


# ---------------- Alertas no WhatsApp ----------------

def whats(texto):
    if not (WHATS_PHONE and WHATS_APIKEY):
        return "sem config"
    url = ("https://api.callmebot.com/whatsapp.php?"
           + urllib.parse.urlencode({"phone": WHATS_PHONE, "text": texto, "apikey": WHATS_APIKEY}))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            r.read()
        return "ok"
    except Exception as e:
        return f"erro: {e}"


@app.route("/ponto2/cron/alertas")
def alertas():
    if not CRON_TOKEN or request.args.get("token") != CRON_TOKEN:
        return "nao autorizado", 403
    tipo = request.args.get("tipo", "saida")
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        if tipo == "entrada":
            cur.execute("""
                SELECT f.nome FROM funcionarios f
                WHERE f.aparece_no_ponto AND NOT f.arquivado AND NOT EXISTS (
                    SELECT 1 FROM pontos p WHERE p.funcionario_id=f.id AND p.dia=%s
                ) ORDER BY f.nome
            """, (hoje(),))
            nomes = [r["nome"].split()[0] for r in cur.fetchall()]
            if not nomes or hoje().weekday() >= 5:
                return "nada a avisar"
            return whats("Ponto InkSugar: ainda sem entrada hoje — " + ", ".join(nomes))
        cur.execute("""
            SELECT f.nome, p.dia FROM pontos p JOIN funcionarios f ON f.id=p.funcionario_id
            WHERE p.saida IS NULL AND NOT f.arquivado ORDER BY p.dia
        """)
        pend = [f"{r['nome'].split()[0]} ({r['dia'].strftime('%d/%m')})" for r in cur.fetchall()]
    if not pend:
        return "nada a avisar"
    return whats("Ponto InkSugar: saída não registrada — " + ", ".join(pend))


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5001)
