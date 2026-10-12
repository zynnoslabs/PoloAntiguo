"""
buscador.py — Polo Antiguo
Fuentes activas:
  - SerpApi: Argentina + España + Uruguay (señales pre-mercado + compradores)
  - eBay API: activa con EBAY_APP_ID en Render (mercado internacional)
"""

import os, json, time, sqlite3, requests, logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buscador")

DB_PATH = os.environ.get("DB_PATH", "db.sqlite")

# ─── DB setup ─────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS vistos (
            id TEXT PRIMARY KEY,
            fuente TEXT,
            visto_en TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS serp_uso (
            fecha TEXT PRIMARY KEY,
            cantidad INTEGER DEFAULT 0
        )
    """)
    con.commit()
    return con

def es_nuevo(con, listing_id: str, fuente: str) -> bool:
    row = con.execute("SELECT 1 FROM vistos WHERE id=? AND fuente=?", (listing_id, fuente)).fetchone()
    return row is None

def marcar_visto(con, listing_id: str, fuente: str):
    ahora = datetime.now(timezone.utc).isoformat()
    con.execute("INSERT OR IGNORE INTO vistos(id,fuente,visto_en) VALUES(?,?,?)", (listing_id, fuente, ahora))
    con.commit()

# ─── SerpApi — queries con país ───────────────────────────────────────────────
# Tuplas: (keyword, gl_country, tipo_forzado_o_None)
# tipo_forzado: si lo sabemos de antemano, lo ponemos; si no, lo detecta el código
#
# Cuota: 100/mes gratis → max 3/dia (90/mes con margen).
# 24 queries rotan de a 3 → cada 8 dias se completa un ciclo.

QUERIES_SERP = [
    # ── ARGENTINA — señales pre-mercado (alguien tiene algo y no sabe qué hacer) ──
    ("heredé reloj antiguo cuánto vale argentina",         "ar", None),
    ("heredé antigüedades plata qué hago argentina",       "ar", None),
    ("heredé contenido casa antigua vendo argentina",      "ar", None),
    ("tasación antigüedades plata buenos aires",           "ar", None),
    ("tasación reloj antiguo longines argentina",          "ar", None),
    ("vendo contenido casa antigua sucesión argentina",    "ar", None),
    ("remate judicial antigüedades buenos aires",          "ar", None),
    ("sucesión vendo antigüedades muebles plata",          "ar", None),
    ("remate antigüedades buenos aires 2026",              "ar", None),
    ("subasta antigüedades argentina 2026",                "ar", None),
    # ── ARGENTINA — compradores buscando piezas ──
    ("busco comprar reloj longines antiguo argentina",     "ar", "comprador"),
    ("compro antigüedades plata buenos aires",             "ar", "comprador"),
    ("busco reloj de bolsillo antiguo argentina",          "ar", "comprador"),
    ("busco mate de plata antiguo argentina",              "ar", "comprador"),
    ("compro monedas antiguas argentina coleccion",        "ar", "comprador"),
    ("busco militaria sable argentino siglo xix",          "ar", "comprador"),
    # ── ARGENTINA — Facebook (indexado por Google) ──
    ("site:facebook.com heredé antigüedades vendo argentina",    "ar", None),
    ("site:facebook.com reloj longines antiguo vendo argentina", "ar", None),
    # ── ESPAÑA — vendedores con piezas que JC puede comprar ──
    ("vendo reloj longines antiguo bolsillo españa",       "es", "vendedor"),
    ("vendo antigüedades plata relojes antiguos españa",   "es", "vendedor"),
    # ── ESPAÑA — compradores a quienes JC puede vender ──
    ("busco reloj longines antiguo comprar españa",        "es", "comprador"),
    ("compro antigüedades plata coleccionista españa",     "es", "comprador"),
    # ── URUGUAY ──
    ("vendo antigüedades reloj plata uruguay",             "uy", None),
    ("busco comprar antigüedades reloj plata uruguay",     "uy", "comprador"),
]


def _detectar_tipo(titulo: str, snippet: str, tipo_forzado) -> str:
    """Detecta si el listado es vendedor, comprador u otro tipo."""
    if tipo_forzado:
        return tipo_forzado
    texto = (titulo + " " + snippet).lower()
    if any(w in texto for w in ["heredé", "herencia", "sucesión", "contenido casa"]):
        return "herencia"
    if any(w in texto for w in ["tasación", "tasar", "qué vale", "cuánto vale"]):
        return "tasacion"
    if any(w in texto for w in ["remate", "subasta", "lote", "martillo"]):
        return "remate"
    if any(w in texto for w in ["busco", "compro", "wanted", "looking for", "se busca"]):
        return "comprador"
    return "vendedor"


def fetch_serpapi(con) -> list[dict]:
    api_key = os.environ.get("SERPAPI_KEY", "")
    if not api_key:
        log.info("SerpApi: sin SERPAPI_KEY, saltando")
        return []

    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = con.execute("SELECT cantidad FROM serp_uso WHERE fecha=?", (hoy,)).fetchone()
    uso_hoy = row[0] if row else 0
    MAX_DIA = 3
    if uso_hoy >= MAX_DIA:
        log.info(f"SerpApi: cuota diaria alcanzada ({uso_hoy}/{MAX_DIA}), saltando")
        return []

    # Rotacion: cada dia toca queries distintas
    row_total = con.execute("SELECT SUM(cantidad) FROM serp_uso").fetchone()
    total_usado = row_total[0] or 0
    n = len(QUERIES_SERP)
    inicio = total_usado % n
    queries_hoy = (QUERIES_SERP + QUERIES_SERP)[inicio: inicio + MAX_DIA - uso_hoy]

    resultados = []
    for kw, gl, tipo_forzado in queries_hoy:
        try:
            r = requests.get("https://serpapi.com/search", params={
                "api_key": api_key,
                "q": kw,
                "hl": "es",
                "gl": gl,
                "num": 10,
            }, timeout=15)
            r.raise_for_status()
            data = r.json()

            con.execute("""
                INSERT INTO serp_uso(fecha, cantidad) VALUES(?,1)
                ON CONFLICT(fecha) DO UPDATE SET cantidad=cantidad+1
            """, (hoy,))
            con.commit()

            for res in data.get("organic_results", []):
                url_res = res.get("link", "")
                lid = f"serp_{abs(hash(url_res + kw))}"
                if not es_nuevo(con, lid, "serpapi"):
                    continue
                marcar_visto(con, lid, "serpapi")
                titulo = res.get("title", "")
                snippet = res.get("snippet", "")
                tipo = _detectar_tipo(titulo, snippet, tipo_forzado)

                resultados.append({
                    "id": lid,
                    "fuente": "Web",
                    "tipo": tipo,
                    "titulo": titulo,
                    "precio": None,
                    "moneda": "ARS" if gl == "ar" else ("EUR" if gl == "es" else "USD"),
                    "url": url_res,
                    "imagen": "",
                    "ubicacion": res.get("source", f"({gl.upper()})"),
                    "keyword": kw,
                    "raw": json.dumps({
                        "title": titulo,
                        "snippet": snippet,
                        "url": url_res,
                        "source": res.get("source"),
                        "country": gl,
                        "tipo_detectado": tipo,
                    }, ensure_ascii=False)[:600],
                })
            time.sleep(1.5)
        except Exception as e:
            log.warning(f"SerpApi '{kw}': {e}")

    log.info(f"SerpApi: {len(resultados)} nuevos (hoy: {uso_hoy + len(queries_hoy)}/{MAX_DIA})")
    return resultados


# ─── eBay API — mercado internacional ─────────────────────────────────────────
# Se activa agregando EBAY_APP_ID en las env vars de Render.
# Registro gratis en: https://developer.ebay.com

KEYWORDS_EBAY = [
    "reloj longines bolsillo antiguo",
    "mate plata argentina antique",
    "sable militar argentino antique",
    "monedas argentinas antiguas",
    "reloj omega antiguo pocket watch",
    "porcelana meissen antigua",
    "gramofono antiguo vitrola",
]


def fetch_ebay(con) -> list[dict]:
    app_id = os.environ.get("EBAY_APP_ID", "")
    if not app_id:
        return []

    resultados = []
    # 2 keywords por ciclo para no saturar
    row_total = con.execute("SELECT SUM(cantidad) FROM serp_uso").fetchone()
    total_usado = row_total[0] or 0
    n = len(KEYWORDS_EBAY)
    inicio = total_usado % n
    kws_hoy = (KEYWORDS_EBAY + KEYWORDS_EBAY)[inicio: inicio + 2]

    for kw in kws_hoy:
        try:
            r = requests.get(
                "https://svcs.ebay.com/services/search/FindingService/v1",
                params={
                    "OPERATION-NAME": "findItemsByKeywords",
                    "SERVICE-VERSION": "1.0.0",
                    "SECURITY-APPNAME": app_id,
                    "RESPONSE-DATA-FORMAT": "JSON",
                    "keywords": kw,
                    "paginationInput.entriesPerPage": "10",
                    "itemFilter(0).name": "Condition",
                    "itemFilter(0).value(0)": "Used",
                    "sortOrder": "BestMatch",
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

            items = (data
                     .get("findItemsByKeywordsResponse", [{}])[0]
                     .get("searchResult", [{}])[0]
                     .get("item", []))

            for it in items:
                item_id = it.get("itemId", [""])[0]
                lid = f"ebay_{item_id}"
                if not es_nuevo(con, lid, "ebay"):
                    continue
                marcar_visto(con, lid, "ebay")

                titulo = it.get("title", [""])[0]
                precio_val = it.get("sellingStatus", [{}])[0].get("currentPrice", [{}])[0].get("__value__", "")
                url_res = it.get("viewItemURL", [""])[0]
                pais = it.get("country", [""])[0]

                resultados.append({
                    "id": lid,
                    "fuente": "eBay",
                    "tipo": "vendedor",
                    "titulo": titulo,
                    "precio": float(precio_val) if precio_val else None,
                    "moneda": "USD",
                    "url": url_res,
                    "imagen": it.get("galleryURL", [""])[0],
                    "ubicacion": pais,
                    "keyword": kw,
                    "raw": json.dumps({
                        "title": titulo,
                        "price_usd": precio_val,
                        "country": pais,
                        "url": url_res,
                    }, ensure_ascii=False)[:600],
                })
            time.sleep(1.0)
        except Exception as e:
            log.warning(f"eBay '{kw}': {e}")

    if resultados:
        log.info(f"eBay: {len(resultados)} nuevos")
    return resultados


# ─── Entry point ──────────────────────────────────────────────────────────────

def buscar_todo() -> list[dict]:
    con = init_db()
    todos = []
    todos += fetch_serpapi(con)
    todos += fetch_ebay(con)
    log.info(f"TOTAL nuevos listados: {len(todos)}")
    return todos


if __name__ == "__main__":
    resultados = buscar_todo()
    print(json.dumps(resultados, ensure_ascii=False, indent=2))
