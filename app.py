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
ALMOCO_PADRAO = 30
JORNADA_LIMITE = 420  # 7h em minutos
ADIANTAMENTO_PADRAO_MIN = 2400  # 40h

# App mora em /ponto2 na mesma VPS do ponto-inksugar original — nginx encaminha
# o caminho inteiro (sem tirar o prefixo), então toda rota e todo link do
# template já nasce com "/ponto2" na frente. Não usar url_for cru sem isso.
app = Flask(__name__, static_url_path="/ponto2/static")
app.secret_key = os.environ.get("SECRET_KEY", "troque-isso")
app.permanent_session_lifetime = timedelta(days=30)

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
    return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def hora_ou_none(txt):
    txt = (txt or "").strip()
    if not txt:
        return None
    try:
        return datetime.strptime(txt, "%H:%M").time()
    except ValueError:
        return None


app.jinja_env.globals.update(hm=hm, hm_sinal=hm_sinal, iniciais=iniciais, brl=brl)


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


def valor_hora_em(cur, fid, data_ref):
    """Valor da hora vigente numa data: a taxa mais recente cujo vigente_desde
    seja <= data_ref. Mudar o valor nunca reescreve o passado — cada alteração
    vale só a partir da data escolhida."""
    cur.execute("""
        SELECT valor_hora FROM taxas WHERE funcionario_id=%s AND vigente_desde<=%s
        ORDER BY vigente_desde DESC LIMIT 1
    """, (fid, data_ref))
    r = cur.fetchone()
    return float(r[0]) if r else None


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

        eventos, trabalhadas, recebidas = extrato_do_mes(cur, fid, ini, fim)
        a_receber = saldo_ate(cur, fid, min(fim, hj))

    ant_ano, ant_mes = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    prox_ano, prox_mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    atual = (ano == hj.year and mes == hj.month)
    nome_mes = f"{MESES[mes - 1]} de {ano}"

    return render_template("mes.html", f=f, eventos=eventos, ini=ini, fim=fim, nome_mes=nome_mes,
                           trabalhadas=trabalhadas, recebidas=recebidas, a_receber=a_receber,
                           ano=ano, mes=mes, atual=atual,
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
    with db() as conn, conn.cursor() as cur:
        if fid:
            if foto:
                cur.execute("""UPDATE funcionarios SET nome=%s,cargo=%s,hibrido=%s,
                               aparece_no_ponto=%s,arquivado=%s,foto=%s,
                               entrada_padrao=%s,saida_padrao=%s,almoco_padrao_min=%s WHERE id=%s""",
                            (nome, cargo, hibrido, aparece_no_ponto, arquivado, foto,
                             entrada_padrao, saida_padrao, almoco_padrao_min, fid))
            else:
                cur.execute("""UPDATE funcionarios SET nome=%s,cargo=%s,hibrido=%s,
                               aparece_no_ponto=%s,arquivado=%s,
                               entrada_padrao=%s,saida_padrao=%s,almoco_padrao_min=%s WHERE id=%s""",
                            (nome, cargo, hibrido, aparece_no_ponto, arquivado,
                             entrada_padrao, saida_padrao, almoco_padrao_min, fid))
        else:
            cur.execute("""INSERT INTO funcionarios (nome,cargo,hibrido,foto,
                           entrada_padrao,saida_padrao,almoco_padrao_min)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (nome, cargo, hibrido, foto, entrada_padrao, saida_padrao, almoco_padrao_min))
    return redirect(url_for("admin"))


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
    return redirect(url_for("admin"))


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
    return redirect(url_for("admin"))


@app.route("/ponto2/admin/taxa/<int:tid>/excluir", methods=["POST"])
@admin_only
def excluir_taxa(tid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM taxas WHERE id=%s", (tid,))
    return redirect(url_for("admin"))


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
    cronologicamente — mesma lógica usada no extrato mensal dela (/ponto/<id>/mes)
    e reaproveitada aqui pro extrato por pessoa da tela de adiantamentos."""
    cur.execute(
        "SELECT * FROM pontos WHERE funcionario_id=%s AND dia BETWEEN %s AND %s ORDER BY dia, entrada",
        (fid, ini, fim),
    )
    pontos_periodo = cur.fetchall()
    cur.execute(
        "SELECT * FROM adiantamentos WHERE funcionario_id=%s AND data BETWEEN %s AND %s ORDER BY data",
        (fid, ini, fim),
    )
    adiant_periodo = cur.fetchall()
    trabalhadas = sum(p["minutos"] or 0 for p in pontos_periodo)
    recebidas = sum(a["minutos"] for a in adiant_periodo)
    eventos = [{"tipo": "trabalho", "data": p["dia"], "dia_semana": DIAS[p["dia"].weekday()], "reg": p}
               for p in pontos_periodo]
    eventos += [{"tipo": "adiantamento", "data": a["data"], "dia_semana": DIAS[a["data"].weekday()], "reg": a}
                for a in adiant_periodo]
    eventos.sort(key=lambda e: (e["data"], 0 if e["tipo"] == "trabalho" else 1))
    return eventos, trabalhadas, recebidas


@app.route("/ponto2/admin/adiantamentos")
@admin_only
def adiantamentos():
    hj = hoje()
    ini_mes, fim_mes = mes_de(hj.year, hj.month)
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM funcionarios WHERE NOT arquivado ORDER BY nome")
        equipe = cur.fetchall()
        linhas = []
        for f in equipe:
            saldo = saldo_ate(cur, f["id"], hj)
            sugestao = min(saldo, ADIANTAMENTO_PADRAO_MIN) if saldo > 0 else 0
            eventos, trabalhadas, recebidas = extrato_do_mes(cur, f["id"], ini_mes, fim_mes)
            linhas.append({"f": f, "saldo": saldo, "sugestao": sugestao,
                           "sugestao_h": round(sugestao / 60, 2),
                           "eventos": eventos, "trabalhadas": trabalhadas, "recebidas": recebidas})
    nome_mes = f"{MESES[hj.month - 1]} de {hj.year}"
    return render_template("adiantamentos.html", linhas=linhas, hoje=hj, nome_mes=nome_mes)


@app.route("/ponto2/admin/adiantamentos/lancar", methods=["POST"])
@admin_only
def lancar_adiantamentos():
    try:
        data_lote = date.fromisoformat(request.form.get("data") or "")
    except ValueError:
        data_lote = hoje()
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM funcionarios WHERE NOT arquivado")
        for (fid,) in cur.fetchall():
            campo = (request.form.get(f"horas_{fid}") or "").strip()
            if not campo:
                continue
            try:
                horas_v = float(campo.replace(",", "."))
            except ValueError:
                continue
            if horas_v <= 0:
                continue
            minutos_v = round(horas_v * 60)
            cur.execute(
                "INSERT INTO adiantamentos (funcionario_id, data, minutos) VALUES (%s,%s,%s)",
                (fid, data_lote, minutos_v),
            )
    return redirect(url_for("adiantamentos"))


@app.route("/ponto2/admin/adiantamento/<int:aid>", methods=["POST"])
@admin_only
def salvar_adiantamento(aid):
    try:
        data_v = date.fromisoformat(request.form["data"])
    except (KeyError, ValueError):
        return redirect(url_for("adiantamentos"))
    try:
        horas_v = float((request.form.get("horas") or "0").replace(",", "."))
    except ValueError:
        horas_v = 0.0
    minutos_v = max(0, round(horas_v * 60))
    obs = (request.form.get("obs") or "").strip()[:200]
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE adiantamentos SET data=%s, minutos=%s, obs=%s WHERE id=%s",
                    (data_v, minutos_v, obs, aid))
    return redirect(url_for("adiantamentos"))


@app.route("/ponto2/admin/adiantamento/<int:aid>/excluir", methods=["POST"])
@admin_only
def excluir_adiantamento(aid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM adiantamentos WHERE id=%s", (aid,))
    return redirect(url_for("adiantamentos"))


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
