"""
buscador.py — Polo Antiguo
Fuente activa: SerpApi (estrategia pre-mercado)
Las demás fuentes (ML, eBay, Reddit) se activan agregando sus variables de entorno en Render.
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

# ─── SerpApi — estrategia pre-mercado ─────────────────────────────────────────
# Buscamos señales de que alguien TIENE algo antes de que lo ponga en ML:
#   • "heredé" / "sucesión"  → heredaron y no saben el valor
#   • "tasación"             → quieren saber cuánto vale antes de vender
#   • "remate"               → subastas de bienes de herencias
# Cuota: 100/mes gratis → máx 3/día (90/mes, con margen de seguridad).
# Las 16 queries rotan en ciclos de 3 → cada día toca queries distintas.

KEYWORDS_SERP = [
    "heredé reloj antiguo cuánto vale argentina",
    "heredé antigüedades plata qué hago argentina",
    "heredé contenido casa antigua vendo argentina",
    "tasación antigüedades plata buenos aires",
    "tasación reloj antiguo longines argentina",
    "donde tasar antigüedades buenos aires",
    "vendo contenido casa antigua sucesión argentina",
    "remate judicial antigüedades buenos aires",
    "sucesión vendo antigüedades muebles plata",
    "remate antigüedades buenos aires 2026",
    "remate contenido casa antigua buenos aires",
    "subasta antigüedades argentina 2026",
    "busco comprar reloj longines antiguo argentina",
    "busco mate plata antiguo argentina compro",
    "site:facebook.com heredé antigüedades vendo argentina",
    "site:facebook.com reloj longines antiguo vendo argentina",
]


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

    # Rotación: cada día toca queries distintas
    row_total = con.execute("SELECT SUM(cantidad) FROM serp_uso").fetchone()
    total_usado = row_total[0] or 0
    n = len(KEYWORDS_SERP)
    inicio = total_usado % n
    kws = (KEYWORDS_SERP + KEYWORDS_SERP)[inicio: inicio + MAX_DIA - uso_hoy]

    resultados = []
    for kw in kws:
        try:
            r = requests.get("https://serpapi.com/search", params={
                "api_key": api_key,
                "q": kw,
                "hl": "es",
                "gl": "ar",
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
                texto = (titulo + " " + snippet).lower()

                if any(w in texto for w in ["heredé", "herencia", "sucesión", "contenido casa"]):
                    tipo = "herencia"
                elif any(w in texto for w in ["tasación", "tasar", "qué vale", "cuánto vale"]):
                    tipo = "tasacion"
                elif any(w in texto for w in ["remate", "subasta", "lote", "martillo"]):
                    tipo = "remate"
                elif any(w in texto for w in ["busco", "compro", "wanted", "looking for"]):
                    tipo = "comprador"
                else:
                    tipo = "vendedor"

                resultados.append({
                    "id": lid,
                    "fuente": "Web",
                    "tipo": tipo,
                    "titulo": titulo,
                    "precio": None,
                    "moneda": "ARS",
                    "url": url_res,
                    "imagen": "",
                    "ubicacion": res.get("source", ""),
                    "keyword": kw,
                    "raw": json.dumps({
                        "title": titulo,
                        "snippet": snippet,
                        "url": url_res,
                        "source": res.get("source"),
                        "tipo_detectado": tipo,
                    }, ensure_ascii=False)[:600],
                })
            time.sleep(1.5)
        except Exception as e:
            log.warning(f"SerpApi '{kw}': {e}")

    log.info(f"SerpApi: {len(resultados)} nuevos (hoy: {uso_hoy + len(kws)}/{MAX_DIA})")
    return resultados

# ─── Entry point ──────────────────────────────────────────────────────────────

def buscar_todo() -> list[dict]:
    con = init_db()
    todos = fetch_serpapi(con)
    log.info(f"TOTAL nuevos listados: {len(todos)}")
    return todos


if __name__ == "__main__":
    resultados = buscar_todo()
    print(json.dumps(resultados, ensure_ascii=False, indent=2))
