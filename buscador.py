"""
buscador.py — Polo Antiguo
Fetcha listados de múltiples fuentes. Groq NO toca esto.
Cada fuente tiene try/except independiente para que un fallo no rompa el resto.
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
    con.commit()
    return con

def es_nuevo(con, listing_id: str, fuente: str) -> bool:
    row = con.execute("SELECT 1 FROM vistos WHERE id=? AND fuente=?", (listing_id, fuente)).fetchone()
    return row is None

def marcar_visto(con, listing_id: str, fuente: str):
    ahora = datetime.now(timezone.utc).isoformat()
    con.execute("INSERT OR IGNORE INTO vistos(id,fuente,visto_en) VALUES(?,?,?)", (listing_id, fuente, ahora))
    con.commit()

# ─── Categorías de interés para Polo Antiguo ──────────────────────────────────

KEYWORDS_ML = [
    "reloj antiguo", "reloj vintage", "longines antiguo",
    "omega antiguo", "sable antiguo", "sable militar",
    "moneda antigua argentina", "billete antiguo argentina",
    "mueble antiguo", "espejo antiguo", "cuadro antiguo",
    "porcelana antigua", "cristalería antigua",
    "mate plata", "bombilla plata",
    "arma antigua coleccion", "pistola antigua",
    "radio antigua", "gramofono", "vitrola antigua",
    "bronce antiguo", "candelabro antiguo",
    "joyeria antigua plata", "medalla antigua militar",
]

KEYWORDS_EBAY = [
    "antique Argentine watch longines",
    "antique Argentine silver mate",
    "Argentine military saber antique",
    "Argentine coins antique",
    "antique Argentine furniture",
]

KEYWORDS_REDDIT = [
    "Argentine antique",
    "Argentine watch vintage",
    "mate silver Argentina",
    "military Argentina antique",
]

KEYWORDS_SERP = [
    "vendo reloj antiguo longines argentina",
    "compro antigüedades plata argentina",
    "vendo sable militar antiguo argentina",
    "busco antigüedades bronce argentina",
]

# ─── MercadoLibre ─────────────────────────────────────────────────────────────

def fetch_mercadolibre(con) -> list[dict]:
    """API oficial ML — gratis, sin auth para búsquedas públicas."""
    resultados = []
    base = "https://api.mercadolibre.com/sites/MLA/search"

    for kw in KEYWORDS_ML:
        try:
            r = requests.get(base, params={"q": kw, "limit": 10}, timeout=10)
            r.raise_for_status()
            items = r.json().get("results", [])
            for it in items:
                lid = f"ml_{it['id']}"
                if not es_nuevo(con, lid, "mercadolibre"):
                    continue
                marcar_visto(con, lid, "mercadolibre")
                resultados.append({
                    "id": lid,
                    "fuente": "MercadoLibre",
                    "tipo": "vendedor",          # alguien VENDE → potencial compra para JC
                    "titulo": it.get("title", ""),
                    "precio": it.get("price"),
                    "moneda": it.get("currency_id", "ARS"),
                    "url": it.get("permalink", ""),
                    "imagen": (it.get("thumbnail") or ""),
                    "ubicacion": it.get("seller_address", {}).get("city", {}).get("name", ""),
                    "keyword": kw,
                    "raw": json.dumps({
                        "title": it.get("title"),
                        "price": it.get("price"),
                        "condition": it.get("condition"),
                        "seller_id": it.get("seller", {}).get("id"),
                        "location": it.get("seller_address", {}),
                    }, ensure_ascii=False)[:500],
                })
            time.sleep(0.4)   # ML pide amabilidad
        except Exception as e:
            log.warning(f"ML '{kw}': {e}")

    log.info(f"MercadoLibre: {len(resultados)} nuevos")
    return resultados


def fetch_ml_preguntas(con) -> list[dict]:
    """
    ML no expone preguntas públicamente por API estándar.
    Buscamos publicaciones en la categoría 'Artículos para Coleccionistas'
    con 'busco' en el título — señal de comprador activo.
    """
    resultados = []
    base = "https://api.mercadolibre.com/sites/MLA/search"
    busco_kws = [kw.replace("reloj antiguo", "busco reloj antiguo")
                 .replace("sable", "busco sable")
                 .replace("moneda", "busco moneda")
                 for kw in ["busco reloj antiguo", "busco sable antiguo",
                             "busco moneda antigua", "busco mate plata",
                             "busco antigüedades", "busco mueble antiguo"]]
    for kw in busco_kws:
        try:
            r = requests.get(base, params={"q": kw, "limit": 8}, timeout=10)
            r.raise_for_status()
            for it in r.json().get("results", []):
                lid = f"ml_busco_{it['id']}"
                if not es_nuevo(con, lid, "mercadolibre"):
                    continue
                marcar_visto(con, lid, "mercadolibre")
                resultados.append({
                    "id": lid,
                    "fuente": "MercadoLibre",
                    "tipo": "comprador",         # alguien BUSCA → potencial venta para JC
                    "titulo": it.get("title", ""),
                    "precio": it.get("price"),
                    "moneda": it.get("currency_id", "ARS"),
                    "url": it.get("permalink", ""),
                    "imagen": (it.get("thumbnail") or ""),
                    "ubicacion": it.get("seller_address", {}).get("city", {}).get("name", ""),
                    "keyword": kw,
                    "raw": json.dumps({
                        "title": it.get("title"),
                        "price": it.get("price"),
                        "condition": it.get("condition"),
                    }, ensure_ascii=False)[:500],
                })
            time.sleep(0.4)
        except Exception as e:
            log.warning(f"ML busco '{kw}': {e}")

    log.info(f"ML compradores: {len(resultados)} nuevos")
    return resultados

# ─── eBay ─────────────────────────────────────────────────────────────────────

def fetch_ebay(con) -> list[dict]:
    """
    eBay Browse API (gratuita con cuenta de desarrollador).
    Requiere EBAY_TOKEN en variables de entorno.
    """
    token = os.environ.get("EBAY_TOKEN", "")
    if not token:
        log.info("eBay: sin EBAY_TOKEN, saltando")
        return []

    resultados = []
    base = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    for kw in KEYWORDS_EBAY:
        try:
            r = requests.get(base, headers=headers,
                             params={"q": kw, "limit": 8, "filter": "conditions:{USED}"},
                             timeout=10)
            r.raise_for_status()
            for it in r.json().get("itemSummaries", []):
                lid = f"ebay_{it['itemId']}"
                if not es_nuevo(con, lid, "ebay"):
                    continue
                marcar_visto(con, lid, "ebay")
                precio_raw = it.get("price", {})
                resultados.append({
                    "id": lid,
                    "fuente": "eBay",
                    "tipo": "vendedor",
                    "titulo": it.get("title", ""),
                    "precio": precio_raw.get("value"),
                    "moneda": precio_raw.get("currency", "USD"),
                    "url": it.get("itemWebUrl", ""),
                    "imagen": (it.get("image", {}).get("imageUrl") or ""),
                    "ubicacion": it.get("itemLocation", {}).get("country", ""),
                    "keyword": kw,
                    "raw": json.dumps({
                        "title": it.get("title"),
                        "price": precio_raw,
                        "condition": it.get("condition"),
                        "seller": it.get("seller", {}),
                    }, ensure_ascii=False)[:500],
                })
            time.sleep(0.5)
        except Exception as e:
            log.warning(f"eBay '{kw}': {e}")

    log.info(f"eBay: {len(resultados)} nuevos")
    return resultados

# ─── Reddit ───────────────────────────────────────────────────────────────────

def fetch_reddit(con) -> list[dict]:
    """
    Reddit public JSON API (sin auth). Subreddits de coleccionismo.
    """
    resultados = []
    subreddits = ["watchexchange", "Coins", "militaria", "antiques", "Silverbugs"]
    headers = {"User-Agent": "PoloAntiguoBuscador/1.0 (contacto@poloantiguo.com)"}

    for sub in subreddits:
        for kw in KEYWORDS_REDDIT:
            try:
                url = f"https://www.reddit.com/r/{sub}/search.json"
                r = requests.get(url, headers=headers,
                                 params={"q": kw, "restrict_sr": 1, "sort": "new", "limit": 10},
                                 timeout=10)
                r.raise_for_status()
                posts = r.json().get("data", {}).get("children", [])
                for p in posts:
                    d = p.get("data", {})
                    # Solo queremos posts de compra/venta
                    flair = (d.get("link_flair_text") or "").lower()
                    title = (d.get("title") or "").lower()
                    if not any(w in title or w in flair for w in
                               ["wts", "wtb", "wtt", "sell", "buy", "want to buy",
                                "want to sell", "fs", "ft", "busco", "vendo"]):
                        continue
                    lid = f"reddit_{d['id']}"
                    if not es_nuevo(con, lid, "reddit"):
                        continue
                    marcar_visto(con, lid, "reddit")
                    tipo = "comprador" if any(w in title or w in flair
                                              for w in ["wtb","want to buy","busco","buy"]) else "vendedor"
                    resultados.append({
                        "id": lid,
                        "fuente": "Reddit",
                        "tipo": tipo,
                        "titulo": d.get("title", ""),
                        "precio": None,
                        "moneda": "USD",
                        "url": f"https://reddit.com{d.get('permalink','')}",
                        "imagen": "",
                        "ubicacion": f"r/{sub}",
                        "keyword": kw,
                        "raw": json.dumps({
                            "title": d.get("title"),
                            "selftext": (d.get("selftext") or "")[:300],
                            "subreddit": sub,
                            "flair": d.get("link_flair_text"),
                            "score": d.get("score"),
                        }, ensure_ascii=False)[:500],
                    })
                time.sleep(1.0)   # Reddit es estricto con rate limits
            except Exception as e:
                log.warning(f"Reddit r/{sub} '{kw}': {e}")

    log.info(f"Reddit: {len(resultados)} nuevos")
    return resultados

# ─── SerpApi ──────────────────────────────────────────────────────────────────

def fetch_serpapi(con) -> list[dict]:
    """
    SerpApi — 250 searches/month gratis.
    Usamos MAX 4/día para no sobrepasar cuota.
    Controlado con SQLite (guardamos timestamp de uso).
    """
    api_key = os.environ.get("SERPAPI_KEY", "")
    if not api_key:
        log.info("SerpApi: sin SERPAPI_KEY, saltando")
        return []

    # Verificar cuántas búsquedas hicimos HOY
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con_serp = sqlite3.connect(DB_PATH)
    con_serp.execute("""
        CREATE TABLE IF NOT EXISTS serp_uso (
            fecha TEXT,
            cantidad INTEGER DEFAULT 0,
            PRIMARY KEY(fecha)
        )
    """)
    con_serp.commit()
    row = con_serp.execute("SELECT cantidad FROM serp_uso WHERE fecha=?", (hoy,)).fetchone()
    uso_hoy = row[0] if row else 0

    MAX_DIA = 4
    if uso_hoy >= MAX_DIA:
        log.info(f"SerpApi: cuota diaria alcanzada ({uso_hoy}/{MAX_DIA}), saltando")
        return []

    resultados = []
    disponibles = MAX_DIA - uso_hoy
    kws_a_usar = KEYWORDS_SERP[:disponibles]

    for kw in kws_a_usar:
        try:
            r = requests.get("https://serpapi.com/search", params={
                "api_key": api_key,
                "q": kw,
                "hl": "es",
                "gl": "ar",
                "num": 8,
            }, timeout=15)
            r.raise_for_status()
            data = r.json()

            # Actualizar contador
            con_serp.execute("""
                INSERT INTO serp_uso(fecha, cantidad) VALUES(?,1)
                ON CONFLICT(fecha) DO UPDATE SET cantidad=cantidad+1
            """, (hoy,))
            con_serp.commit()

            for res in data.get("organic_results", []):
                url_res = res.get("link", "")
                lid = f"serp_{abs(hash(url_res))}"
                if not es_nuevo(con, lid, "serpapi"):
                    continue
                marcar_visto(con, lid, "serpapi")
                titulo = res.get("title", "")
                snippet = res.get("snippet", "")
                tipo = "comprador" if any(w in titulo.lower() or w in snippet.lower()
                                          for w in ["busco","compro","wanted","wtb","looking for"]) else "vendedor"
                resultados.append({
                    "id": lid,
                    "fuente": "Web",
                    "tipo": tipo,
                    "titulo": titulo,
                    "precio": None,
                    "moneda": "ARS",
                    "url": url_res,
                    "imagen": "",
                    "ubicacion": "",
                    "keyword": kw,
                    "raw": json.dumps({
                        "title": titulo,
                        "snippet": snippet,
                        "url": url_res,
                        "source": res.get("source"),
                    }, ensure_ascii=False)[:500],
                })
            time.sleep(1.0)
        except Exception as e:
            log.warning(f"SerpApi '{kw}': {e}")

    log.info(f"SerpApi: {len(resultados)} nuevos")
    return resultados

# ─── OLX Argentina ────────────────────────────────────────────────────────────

def fetch_olx(con) -> list[dict]:
    """
    OLX Argentina — scraping liviano con requests + heurísticas de título.
    No hay API oficial. Usamos JSON embebido en la página.
    """
    resultados = []
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "es-AR,es;q=0.9",
    })

    olx_kws = [
        "antiguedades", "reloj antiguo", "sable antiguo",
        "mueble antiguo", "porcelana antigua", "moneda antigua",
    ]

    for kw in olx_kws:
        try:
            r = session.get(
                "https://www.olx.com.ar/items/q-" + kw.replace(" ", "-"),
                timeout=12
            )
            if r.status_code != 200:
                continue

            # OLX embeds __NEXT_DATA__ JSON
            import re
            match = re.search(r'id="__NEXT_DATA__" type="application/json">(.+?)</script>', r.text)
            if not match:
                continue

            next_data = json.loads(match.group(1))
            ads = (next_data.get("props", {})
                            .get("pageProps", {})
                            .get("ads", []))

            for ad in ads[:8]:
                lid = f"olx_{ad.get('id', abs(hash(ad.get('url',''))))}"
                if not es_nuevo(con, lid, "olx"):
                    continue
                marcar_visto(con, lid, "olx")
                titulo = ad.get("title", "")
                precio_data = ad.get("price", {}) or {}
                tipo = "comprador" if any(w in titulo.lower()
                                          for w in ["busco","compro","necesito"]) else "vendedor"
                resultados.append({
                    "id": lid,
                    "fuente": "OLX",
                    "tipo": tipo,
                    "titulo": titulo,
                    "precio": precio_data.get("value"),
                    "moneda": "ARS",
                    "url": "https://www.olx.com.ar" + ad.get("url", ""),
                    "imagen": (ad.get("images", [{}])[0].get("url") or "")
                              if ad.get("images") else "",
                    "ubicacion": ad.get("location", {}).get("names", {}).get("neighbourhood", ""),
                    "keyword": kw,
                    "raw": json.dumps({
                        "title": titulo,
                        "price": precio_data,
                        "description": (ad.get("description") or "")[:200],
                    }, ensure_ascii=False)[:500],
                })
            time.sleep(1.5)
        except Exception as e:
            log.warning(f"OLX '{kw}': {e}")

    log.info(f"OLX: {len(resultados)} nuevos")
    return resultados

# ─── Entry point ──────────────────────────────────────────────────────────────

def buscar_todo() -> list[dict]:
    con = init_db()
    todos = []
    todos += fetch_mercadolibre(con)
    todos += fetch_ml_preguntas(con)
    todos += fetch_ebay(con)
    todos += fetch_reddit(con)
    todos += fetch_serpapi(con)
    todos += fetch_olx(con)
    log.info(f"TOTAL nuevos listados: {len(todos)}")
    return todos


if __name__ == "__main__":
    resultados = buscar_todo()
    print(json.dumps(resultados, ensure_ascii=False, indent=2))
