import os
import re
import unicodedata
from io import BytesIO
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import pandas as pd
import psycopg
from psycopg.rows import dict_row
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "controle-estoque-ml-dev")
DATABASE_URL = os.environ.get("DATABASE_URL")


def normalize_database_url(url: str) -> str:
    if not url:
        raise RuntimeError("DATABASE_URL não configurada.")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q.setdefault("sslmode", "require")
    return urlunparse(parsed._replace(query=urlencode(q)))


def get_conn():
    return psycopg.connect(normalize_database_url(DATABASE_URL), row_factory=dict_row)


def dec(v, default=0):
    if v is None:
        return Decimal(default)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)) and not pd.isna(v):
        return Decimal(str(v))
    s = str(v).strip()
    if s == "" or s.lower() in {"nan", "none", "-"}:
        return Decimal(default)
    s = s.replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal(default)


def txt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def norm_col(c):
    s = str(c).strip().lower()
    # Remove acentos e símbolos de forma segura.
    # Evita erro de str.maketrans quando a quantidade de caracteres não bate.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("º", "").replace("°", "").replace("#", "")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def first_col(df, *names):
    norm_map = {norm_col(c): c for c in df.columns}
    for n in names:
        key = norm_col(n)
        if key in norm_map:
            return norm_map[key]
    return None


def get_value(row, col, default=""):
    if not col or col not in row:
        return default
    v = row[col]
    if pd.isna(v):
        return default
    return v


def find_header_row(path, sheet_name=0, marker="N.º de venda"):
    raw = pd.read_excel(path, sheet_name=sheet_name, header=None, nrows=15)
    for i in range(len(raw)):
        vals = [str(x).strip() for x in raw.iloc[i].tolist()]
        if marker in vals:
            return i
    return 0


def make_unique_columns(cols):
    seen = {}
    out = []
    for c in cols:
        base = str(c).strip()
        if base in seen:
            seen[base] += 1
            out.append(f"{base}.{seen[base]}")
        else:
            seen[base] = 0
            out.append(base)
    return out


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS produtos (
                    id SERIAL PRIMARY KEY,
                    codigo TEXT UNIQUE NOT NULL,
                    codigos_alternativos TEXT DEFAULT '',
                    sku TEXT DEFAULT '',
                    descricao TEXT NOT NULL,
                    quantidade NUMERIC(12,3) NOT NULL DEFAULT 0,
                    estoque_minimo NUMERIC(12,3) NOT NULL DEFAULT 0,
                    localizacao TEXT DEFAULT '',
                    custo_produto NUMERIC(12,2) NOT NULL DEFAULT 0,
                    ativo BOOLEAN NOT NULL DEFAULT TRUE,
                    observacoes TEXT DEFAULT '',
                    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS anuncios_ml (
                    id SERIAL PRIMARY KEY,
                    codigo_anuncio_ml TEXT UNIQUE NOT NULL,
                    sku_anuncio TEXT DEFAULT '',
                    titulo TEXT DEFAULT '',
                    forma_entrega TEXT DEFAULT '',
                    status TEXT DEFAULT '',
                    ativo BOOLEAN NOT NULL DEFAULT TRUE,
                    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS composicao_anuncio (
                    id SERIAL PRIMARY KEY,
                    anuncio_id INTEGER NOT NULL REFERENCES anuncios_ml(id) ON DELETE CASCADE,
                    produto_id INTEGER NOT NULL REFERENCES produtos(id) ON DELETE CASCADE,
                    quantidade_por_venda NUMERIC(12,3) NOT NULL DEFAULT 1,
                    UNIQUE(anuncio_id, produto_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vendas_ml (
                    id SERIAL PRIMARY KEY,
                    chave_venda TEXT UNIQUE NOT NULL,
                    numero_venda TEXT NOT NULL,
                    data_venda TIMESTAMP NULL,
                    codigo_anuncio_ml TEXT DEFAULT '',
                    sku TEXT DEFAULT '',
                    titulo TEXT DEFAULT '',
                    quantidade_vendida NUMERIC(12,3) NOT NULL DEFAULT 0,
                    valor_unitario NUMERIC(12,2) NOT NULL DEFAULT 0,
                    forma_entrega TEXT DEFAULT '',
                    status_venda TEXT DEFAULT '',
                    is_full BOOLEAN NOT NULL DEFAULT FALSE,
                    processada BOOLEAN NOT NULL DEFAULT FALSE,
                    mensagem TEXT DEFAULT '',
                    arquivo TEXT DEFAULT '',
                    importado_em TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS movimentacoes_estoque (
                    id SERIAL PRIMARY KEY,
                    produto_id INTEGER NOT NULL REFERENCES produtos(id) ON DELETE CASCADE,
                    tipo TEXT NOT NULL,
                    quantidade NUMERIC(12,3) NOT NULL,
                    saldo_anterior NUMERIC(12,3) NOT NULL,
                    saldo_novo NUMERIC(12,3) NOT NULL,
                    origem TEXT DEFAULT '',
                    referencia TEXT DEFAULT '',
                    observacao TEXT DEFAULT '',
                    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_produtos_codigo ON produtos(codigo)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_anuncios_codigo ON anuncios_ml(codigo_anuncio_ml)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vendas_codigo ON vendas_ml(codigo_anuncio_ml)")
            conn.commit()


@app.before_request
def before_request():
    init_db()


@app.route("/")
def index():
    busca = request.args.get("busca", "").strip()
    filtro = request.args.get("filtro", "todos")
    where = ["p.ativo = TRUE"]
    params = []
    if busca:
        like = f"%{busca}%"
        where.append("(p.codigo ILIKE %s OR p.sku ILIKE %s OR p.descricao ILIKE %s OR p.codigos_alternativos ILIKE %s)")
        params += [like, like, like, like]
    if filtro == "baixo":
        where.append("p.estoque_minimo > 0 AND p.quantidade <= p.estoque_minimo")
    if filtro == "negativo":
        where.append("p.quantidade < 0")
    sql_where = " AND ".join(where)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.*,
                   COALESCE((SELECT COUNT(*) FROM composicao_anuncio ca WHERE ca.produto_id=p.id),0) AS qtd_anuncios
            FROM produtos p
            WHERE {sql_where}
            ORDER BY
                CASE WHEN p.estoque_minimo > 0 AND p.quantidade <= p.estoque_minimo THEN 0 ELSE 1 END,
                p.descricao
            LIMIT 500
        """, params)
        produtos = cur.fetchall()
        cur.execute("SELECT COUNT(*) total, COALESCE(SUM(quantidade),0) qtd FROM produtos WHERE ativo=TRUE")
        k_prod = cur.fetchone()
        cur.execute("SELECT COUNT(*) total FROM produtos WHERE ativo=TRUE AND estoque_minimo > 0 AND quantidade <= estoque_minimo")
        baixo = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) total FROM anuncios_ml WHERE ativo=TRUE")
        anuncios = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) total FROM vendas_ml WHERE importado_em::date = CURRENT_DATE")
        vendas_hoje = cur.fetchone()["total"]
    return render_template("index.html", produtos=produtos, busca=busca, filtro=filtro, k_prod=k_prod, baixo=baixo, anuncios=anuncios, vendas_hoje=vendas_hoje)


@app.route("/produto/novo", methods=["GET", "POST"])
def produto_novo():
    return produto_form(None)


@app.route("/produto/<int:produto_id>", methods=["GET", "POST"])
def produto_editar(produto_id):
    return produto_form(produto_id)


def produto_form(produto_id):
    with get_conn() as conn, conn.cursor() as cur:
        produto = None
        if produto_id:
            cur.execute("SELECT * FROM produtos WHERE id=%s", (produto_id,))
            produto = cur.fetchone()
            if not produto:
                flash("Produto não encontrado.", "danger")
                return redirect(url_for("index"))
        if request.method == "POST":
            codigo = request.form.get("codigo", "").strip()
            descricao = request.form.get("descricao", "").strip()
            if not codigo or not descricao:
                flash("Código e descrição são obrigatórios.", "danger")
                return render_template("produto.html", produto=produto)
            dados = {
                "codigo": codigo,
                "codigos_alternativos": request.form.get("codigos_alternativos", "").strip(),
                "sku": request.form.get("sku", "").strip(),
                "descricao": descricao,
                "quantidade": dec(request.form.get("quantidade")),
                "estoque_minimo": dec(request.form.get("estoque_minimo")),
                "localizacao": request.form.get("localizacao", "").strip(),
                "custo_produto": dec(request.form.get("custo_produto")),
                "observacoes": request.form.get("observacoes", "").strip(),
            }
            if produto_id:
                cur.execute("""
                    UPDATE produtos SET codigo=%s, codigos_alternativos=%s, sku=%s, descricao=%s,
                    quantidade=%s, estoque_minimo=%s, localizacao=%s, custo_produto=%s,
                    observacoes=%s, atualizado_em=NOW()
                    WHERE id=%s
                """, (*dados.values(), produto_id))
            else:
                cur.execute("""
                    INSERT INTO produtos (codigo, codigos_alternativos, sku, descricao, quantidade, estoque_minimo,
                    localizacao, custo_produto, observacoes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, tuple(dados.values()))
            conn.commit()
            flash("Produto salvo com sucesso.", "success")
            return redirect(url_for("index"))
        return render_template("produto.html", produto=produto)


@app.route("/produto/<int:produto_id>/ajuste", methods=["POST"])
def ajuste_estoque(produto_id):
    tipo = request.form.get("tipo", "ajuste")
    qtd = dec(request.form.get("quantidade"))
    obs = request.form.get("observacao", "").strip()
    sinal = Decimal(1)
    if tipo in ["saida", "ajuste_negativo"]:
        sinal = Decimal(-1)
    delta = qtd * sinal
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT quantidade FROM produtos WHERE id=%s FOR UPDATE", (produto_id,))
        p = cur.fetchone()
        if not p:
            flash("Produto não encontrado.", "danger")
            return redirect(url_for("index"))
        anterior = dec(p["quantidade"])
        novo = anterior + delta
        cur.execute("UPDATE produtos SET quantidade=%s, atualizado_em=NOW() WHERE id=%s", (novo, produto_id))
        cur.execute("""
            INSERT INTO movimentacoes_estoque (produto_id, tipo, quantidade, saldo_anterior, saldo_novo, origem, observacao)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (produto_id, tipo, delta, anterior, novo, "ajuste_manual", obs))
        conn.commit()
    flash("Estoque ajustado.", "success")
    return redirect(url_for("produto_editar", produto_id=produto_id))


@app.route("/anuncios")
def anuncios():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT a.*, COUNT(ca.id) AS itens_composicao
            FROM anuncios_ml a
            LEFT JOIN composicao_anuncio ca ON ca.anuncio_id=a.id
            WHERE a.ativo=TRUE
            GROUP BY a.id
            ORDER BY a.titulo, a.codigo_anuncio_ml
        """)
        rows = cur.fetchall()
    return render_template("anuncios.html", anuncios=rows)


@app.route("/anuncio/<int:anuncio_id>")
def anuncio_detalhe(anuncio_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM anuncios_ml WHERE id=%s", (anuncio_id,))
        anuncio = cur.fetchone()
        cur.execute("""
            SELECT ca.*, p.codigo, p.descricao, p.quantidade
            FROM composicao_anuncio ca
            JOIN produtos p ON p.id=ca.produto_id
            WHERE ca.anuncio_id=%s ORDER BY p.descricao
        """, (anuncio_id,))
        comps = cur.fetchall()
        cur.execute("SELECT id, codigo, descricao FROM produtos WHERE ativo=TRUE ORDER BY descricao")
        produtos = cur.fetchall()
    return render_template("anuncio.html", anuncio=anuncio, comps=comps, produtos=produtos)


@app.route("/anuncio/<int:anuncio_id>/composicao", methods=["POST"])
def add_composicao(anuncio_id):
    produto_id = int(request.form.get("produto_id"))
    qtd = dec(request.form.get("quantidade_por_venda"), 1)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO composicao_anuncio (anuncio_id, produto_id, quantidade_por_venda)
            VALUES (%s,%s,%s)
            ON CONFLICT (anuncio_id, produto_id)
            DO UPDATE SET quantidade_por_venda=EXCLUDED.quantidade_por_venda
        """, (anuncio_id, produto_id, qtd))
        conn.commit()
    flash("Composição salva.", "success")
    return redirect(url_for("anuncio_detalhe", anuncio_id=anuncio_id))


@app.route("/composicao/<int:comp_id>/excluir", methods=["POST"])
def excluir_composicao(comp_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT anuncio_id FROM composicao_anuncio WHERE id=%s", (comp_id,))
        r = cur.fetchone()
        if r:
            anuncio_id = r["anuncio_id"]
            cur.execute("DELETE FROM composicao_anuncio WHERE id=%s", (comp_id,))
            conn.commit()
            flash("Item removido da composição.", "success")
            return redirect(url_for("anuncio_detalhe", anuncio_id=anuncio_id))
    return redirect(url_for("anuncios"))


@app.route("/modelo-produtos")
def modelo_produtos():
    cols = [
        "CODIGO_PRODUTO", "CODIGOS_ALTERNATIVOS", "SKU_PRODUTO", "DESCRICAO_PRODUTO", "QUANTIDADE_ESTOQUE",
        "ESTOQUE_MINIMO", "LOCALIZACAO", "CUSTO_PRODUTO", "OBSERVACOES",
        "CODIGO_ANUNCIO_ML", "SKU_ANUNCIO", "TITULO_ANUNCIO", "FORMA_ENTREGA", "STATUS_ANUNCIO", "QTD_POR_VENDA"
    ]
    exemplo = [{
        "CODIGO_PRODUTO": "COIFA-KOMBI",
        "CODIGOS_ALTERNATIVOS": "ALT1; ALT2",
        "SKU_PRODUTO": "COIFA-KOMBI",
        "DESCRICAO_PRODUTO": "Coifa semi eixo Kombi",
        "QUANTIDADE_ESTOQUE": 10,
        "ESTOQUE_MINIMO": 2,
        "LOCALIZACAO": "Prateleira ML A1",
        "CUSTO_PRODUTO": 15.50,
        "OBSERVACOES": "Produto simples",
        "CODIGO_ANUNCIO_ML": "MLB0000000000",
        "SKU_ANUNCIO": "KIT-COIFA-KOMBI",
        "TITULO_ANUNCIO": "Kit Coifa Semi Eixo Kombi Par",
        "FORMA_ENTREGA": "Mercado Envios",
        "STATUS_ANUNCIO": "Ativo",
        "QTD_POR_VENDA": 2,
    }]
    df = pd.DataFrame(exemplo, columns=cols)
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Produtos_Anuncios")
        ws = writer.sheets["Produtos_Anuncios"]
        for i, c in enumerate(cols):
            ws.set_column(i, i, max(16, len(c)+2))
    bio.seek(0)
    return send_file(bio, as_attachment=True, download_name="modelo_produtos_anuncios_ml.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/importar-produtos", methods=["GET", "POST"])
def importar_produtos():
    resultado = None
    if request.method == "POST":
        f = request.files.get("arquivo")
        if not f:
            flash("Selecione um arquivo Excel.", "danger")
            return redirect(url_for("importar_produtos"))
        df = pd.read_excel(f, sheet_name=0)
        # Remove linhas totalmente vazias
        df = df.dropna(how="all")
        col_codigo = first_col(df, "CODIGO_PRODUTO", "CODIGO", "Código")
        col_alt = first_col(df, "CODIGOS_ALTERNATIVOS", "Códigos alternativos", "CODIGOS ALTERNATIVOS")
        col_sku_prod = first_col(df, "SKU_PRODUTO", "SKU")
        col_desc = first_col(df, "DESCRICAO_PRODUTO", "DESCRIÇÃO_PRODUTO", "DESCRICAO", "Descrição")
        col_qtd = first_col(df, "QUANTIDADE_ESTOQUE", "QUANTIDADE", "Quantidade")
        col_min = first_col(df, "ESTOQUE_MINIMO", "Estoque mínimo")
        col_loc = first_col(df, "LOCALIZACAO", "Localização")
        col_custo = first_col(df, "CUSTO_PRODUTO", "Custo produto")
        col_obs = first_col(df, "OBSERVACOES", "Observações", "OBSERVACAO")
        col_anuncio = first_col(df, "CODIGO_ANUNCIO_ML", "# de anúncio", "codigo anuncio_ml", "Código anúncio ML")
        col_sku_an = first_col(df, "SKU_ANUNCIO", "SKU_ANÚNCIO")
        col_titulo = first_col(df, "TITULO_ANUNCIO", "Título do anúncio", "TITULO")
        col_entrega = first_col(df, "FORMA_ENTREGA", "Forma de entrega")
        col_status = first_col(df, "STATUS_ANUNCIO", "Status")
        col_qtd_venda = first_col(df, "QTD_POR_VENDA", "QUANTIDADE_POR_VENDA", "Quantidade por venda")
        if not col_codigo or not col_desc:
            flash("A planilha precisa ter ao menos CODIGO_PRODUTO e DESCRICAO_PRODUTO.", "danger")
            return redirect(url_for("importar_produtos"))
        criados = atualizados = anuncios_criados = comps = 0
        with get_conn() as conn, conn.cursor() as cur:
            for _, row in df.iterrows():
                codigo = txt(get_value(row, col_codigo)).upper()
                desc = txt(get_value(row, col_desc))
                if not codigo or not desc:
                    continue
                cur.execute("SELECT id FROM produtos WHERE codigo=%s", (codigo,))
                p = cur.fetchone()
                dados = (
                    txt(get_value(row, col_alt)), txt(get_value(row, col_sku_prod)), desc,
                    dec(get_value(row, col_qtd)), dec(get_value(row, col_min)), txt(get_value(row, col_loc)),
                    dec(get_value(row, col_custo)), txt(get_value(row, col_obs)), codigo
                )
                if p:
                    cur.execute("""
                        UPDATE produtos SET codigos_alternativos=%s, sku=%s, descricao=%s, quantidade=%s,
                        estoque_minimo=%s, localizacao=%s, custo_produto=%s, observacoes=%s, atualizado_em=NOW()
                        WHERE codigo=%s RETURNING id
                    """, dados)
                    produto_id = cur.fetchone()["id"]
                    atualizados += 1
                else:
                    cur.execute("""
                        INSERT INTO produtos (codigos_alternativos, sku, descricao, quantidade, estoque_minimo,
                        localizacao, custo_produto, observacoes, codigo)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    """, dados)
                    produto_id = cur.fetchone()["id"]
                    criados += 1
                codigo_anuncio = txt(get_value(row, col_anuncio)).upper()
                if codigo_anuncio:
                    cur.execute("SELECT id FROM anuncios_ml WHERE codigo_anuncio_ml=%s", (codigo_anuncio,))
                    a = cur.fetchone()
                    if a:
                        anuncio_id = a["id"]
                        cur.execute("""
                            UPDATE anuncios_ml SET sku_anuncio=%s, titulo=%s, forma_entrega=%s, status=%s,
                            ativo=TRUE, atualizado_em=NOW() WHERE id=%s
                        """, (txt(get_value(row, col_sku_an)), txt(get_value(row, col_titulo)), txt(get_value(row, col_entrega)), txt(get_value(row, col_status)), anuncio_id))
                    else:
                        cur.execute("""
                            INSERT INTO anuncios_ml (codigo_anuncio_ml, sku_anuncio, titulo, forma_entrega, status)
                            VALUES (%s,%s,%s,%s,%s) RETURNING id
                        """, (codigo_anuncio, txt(get_value(row, col_sku_an)), txt(get_value(row, col_titulo)), txt(get_value(row, col_entrega)), txt(get_value(row, col_status))))
                        anuncio_id = cur.fetchone()["id"]
                        anuncios_criados += 1
                    qtdv = dec(get_value(row, col_qtd_venda), 1)
                    if qtdv <= 0:
                        qtdv = Decimal(1)
                    cur.execute("""
                        INSERT INTO composicao_anuncio (anuncio_id, produto_id, quantidade_por_venda)
                        VALUES (%s,%s,%s)
                        ON CONFLICT (anuncio_id, produto_id)
                        DO UPDATE SET quantidade_por_venda=EXCLUDED.quantidade_por_venda
                    """, (anuncio_id, produto_id, qtdv))
                    comps += 1
            conn.commit()
        resultado = {"criados": criados, "atualizados": atualizados, "anuncios_criados": anuncios_criados, "composicoes": comps}
        flash("Importação concluída.", "success")
    return render_template("importar_produtos.html", resultado=resultado)


def importar_vendas_excel(file_storage):
    # Salva em memória para permitir tentativa de leitura múltipla
    data = file_storage.read()
    bio = BytesIO(data)
    xls = pd.ExcelFile(bio)
    sheet = "Vendas BR" if "Vendas BR" in xls.sheet_names else xls.sheet_names[0]
    bio.seek(0)
    header = find_header_row(bio, sheet_name=sheet, marker="N.º de venda")
    bio.seek(0)
    df = pd.read_excel(bio, sheet_name=sheet, header=header)
    df.columns = make_unique_columns(df.columns)
    df = df.dropna(how="all")
    return df, sheet, header


@app.route("/importar-vendas", methods=["GET", "POST"])
def importar_vendas():
    resultado = None
    if request.method == "POST":
        f = request.files.get("arquivo")
        if not f:
            flash("Selecione o relatório de vendas.", "danger")
            return redirect(url_for("importar_vendas"))
        df, sheet, header = importar_vendas_excel(f)
        col_venda = first_col(df, "N.º de venda", "Nº de venda", "Numero de venda")
        col_data = first_col(df, "Data da venda")
        col_status = first_col(df, "Descrição do status", "Descricao do status")
        col_unid = first_col(df, "Unidades")
        col_sku = first_col(df, "SKU")
        col_anuncio = first_col(df, "# de anúncio", "Código anúncio", "codigo_anuncio_ml")
        col_titulo = first_col(df, "Título do anúncio", "Titulo do anuncio")
        col_preco = first_col(df, "Preço unitário de venda do anúncio (BRL)", "Preco unitario de venda do anuncio BRL")
        col_entrega = first_col(df, "Forma de entrega")
        if not col_venda or not col_anuncio or not col_unid or not col_entrega:
            flash("Não consegui identificar as colunas necessárias no relatório de vendas.", "danger")
            return render_template("importar_vendas.html", resultado=None, colunas=df.columns.tolist())
        total = len(df)
        full_ignoradas = processadas = pendentes = duplicadas = 0
        msgs = []
        arquivo = f.filename
        with get_conn() as conn, conn.cursor() as cur:
            for _, row in df.iterrows():
                numero = txt(get_value(row, col_venda))
                codigo_anuncio = txt(get_value(row, col_anuncio)).upper()
                if not numero or not codigo_anuncio:
                    continue
                qtd_vendida = dec(get_value(row, col_unid), 0)
                forma = txt(get_value(row, col_entrega))
                is_full = "mercado envios full" in forma.lower()
                chave = f"{numero}|{codigo_anuncio}|{txt(get_value(row, col_sku))}|{qtd_vendida}"
                cur.execute("SELECT id FROM vendas_ml WHERE chave_venda=%s", (chave,))
                if cur.fetchone():
                    duplicadas += 1
                    continue
                data_venda = None
                try:
                    dv = get_value(row, col_data)
                    if pd.notna(dv):
                        data_venda = pd.to_datetime(dv).to_pydatetime()
                except Exception:
                    data_venda = None
                cur.execute("""
                    INSERT INTO vendas_ml (chave_venda, numero_venda, data_venda, codigo_anuncio_ml, sku, titulo,
                    quantidade_vendida, valor_unitario, forma_entrega, status_venda, is_full, processada, mensagem, arquivo)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,'',%s) RETURNING id
                """, (chave, numero, data_venda, codigo_anuncio, txt(get_value(row, col_sku)), txt(get_value(row, col_titulo)), qtd_vendida,
                      dec(get_value(row, col_preco),0), forma, txt(get_value(row, col_status)), is_full, arquivo))
                venda_id = cur.fetchone()["id"]
                if is_full:
                    cur.execute("UPDATE vendas_ml SET processada=TRUE, mensagem=%s WHERE id=%s", ("Venda Full: não baixa estoque ML separado. Baixa ocorre no envio ao Full.", venda_id))
                    full_ignoradas += 1
                    continue
                cur.execute("SELECT id FROM anuncios_ml WHERE codigo_anuncio_ml=%s", (codigo_anuncio,))
                anuncio = cur.fetchone()
                if not anuncio:
                    msg = "Pendente: anúncio não cadastrado/composição não encontrada."
                    cur.execute("UPDATE vendas_ml SET mensagem=%s WHERE id=%s", (msg, venda_id))
                    pendentes += 1
                    msgs.append(f"{codigo_anuncio}: {msg}")
                    continue
                cur.execute("""
                    SELECT ca.quantidade_por_venda, p.id produto_id, p.codigo, p.descricao, p.quantidade
                    FROM composicao_anuncio ca
                    JOIN produtos p ON p.id=ca.produto_id
                    WHERE ca.anuncio_id=%s
                """, (anuncio["id"],))
                comps = cur.fetchall()
                if not comps:
                    msg = "Pendente: anúncio sem composição cadastrada."
                    cur.execute("UPDATE vendas_ml SET mensagem=%s WHERE id=%s", (msg, venda_id))
                    pendentes += 1
                    msgs.append(f"{codigo_anuncio}: {msg}")
                    continue
                for c in comps:
                    baixa = dec(c["quantidade_por_venda"],1) * qtd_vendida
                    anterior = dec(c["quantidade"])
                    novo = anterior - baixa
                    cur.execute("UPDATE produtos SET quantidade=%s, atualizado_em=NOW() WHERE id=%s", (novo, c["produto_id"]))
                    cur.execute("""
                        INSERT INTO movimentacoes_estoque (produto_id, tipo, quantidade, saldo_anterior, saldo_novo, origem, referencia, observacao)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (c["produto_id"], "venda_ml", -baixa, anterior, novo, "relatorio_vendas_ml", numero, f"Venda anúncio {codigo_anuncio}"))
                cur.execute("UPDATE vendas_ml SET processada=TRUE, mensagem=%s WHERE id=%s", ("Baixa realizada.", venda_id))
                processadas += 1
            conn.commit()
        resultado = {"total": total, "processadas": processadas, "full_ignoradas": full_ignoradas, "pendentes": pendentes, "duplicadas": duplicadas, "mensagens": msgs[:20], "sheet": sheet, "header": header+1}
        flash("Relatório de vendas importado.", "success")
    return render_template("importar_vendas.html", resultado=resultado)


@app.route("/vendas")
def vendas():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM vendas_ml ORDER BY importado_em DESC, id DESC LIMIT 300
        """)
        rows = cur.fetchall()
    return render_template("vendas.html", vendas=rows)


@app.route("/movimentacoes")
def movimentacoes():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT m.*, p.codigo, p.descricao
            FROM movimentacoes_estoque m
            JOIN produtos p ON p.id=m.produto_id
            ORDER BY m.criado_em DESC, m.id DESC
            LIMIT 500
        """)
        rows = cur.fetchall()
    return render_template("movimentacoes.html", rows=rows)


@app.route("/backup")
def backup():
    bio = BytesIO()
    with get_conn() as conn:
        tabelas = ["produtos", "anuncios_ml", "composicao_anuncio", "vendas_ml", "movimentacoes_estoque"]
        with pd.ExcelWriter(bio, engine="xlsxwriter") as writer:
            for t in tabelas:
                df = pd.read_sql(f"SELECT * FROM {t}", conn)
                df.to_excel(writer, index=False, sheet_name=t[:31])
    bio.seek(0)
    nome = f"backup_controle_estoque_ml_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(bio, as_attachment=True, download_name=nome, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    app.run(debug=True)
