"""
app.py — Polo Antiguo Buscador
Flask API + cron job integrado.
El dashboard de Netlify fetchea desde /api/oportunidades.

Deploy en Render:
- Tipo: Web Service
- Build Command: pip install -r requirements.txt
- Start Command: gunicorn app:app --workers 1 --timeout 120
- Variables: GROQ_API_KEY, EBAY_TOKEN (opcional), SERPAPI_KEY (opcional)
"""

import os, json, sqlite3, logging, threading
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

from buscador import buscar_todo, DB_PATH, init_db
from analizador import analizar_todos

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("app")

app = Flask(__name__)
CORS(app, origins="*")   # ### CAMBIO AQUÍ ### wildcard — acepta file://, Netlify y localhost

# ─── Estado en memoria (persiste en SQLite) ────────────────────────────────────

_lock = threading.Lock()
_ultimo_run = None
_estado_run = "esperando"    # "corriendo" | "ok" | "error" | "esperando"

# ─── SQLite — tabla de oportunidades ──────────────────────────────────────────

def init_oportunidades_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS oportunidades (
            id TEXT PRIMARY KEY,
            fuente TEXT,
            tipo TEXT,
            titulo TEXT,
            precio REAL,
            moneda TEXT,
            url TEXT,
            imagen TEXT,
            ubicacion TEXT,
            categoria TEXT,
            score INTEGER,
            razon TEXT,
            mensaje_wa TEXT,
            estado TEXT DEFAULT 'nuevo',
            encontrado_en TEXT
        )
    """)
    con.commit()
    return con

def guardar_oportunidades(items: list[dict]):
    con = init_oportunidades_db()
    ahora = datetime.now(timezone.utc).isoformat()
    for it in items:
        try:
            con.execute("""
                INSERT OR IGNORE INTO oportunidades
                (id,fuente,tipo,titulo,precio,moneda,url,imagen,ubicacion,
                 categoria,score,razon,mensaje_wa,estado,encontrado_en)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'nuevo',?)
            """, (
                it.get("id"), it.get("fuente"), it.get("tipo"),
                it.get("titulo"), it.get("precio"), it.get("moneda","ARS"),
                it.get("url"), it.get("imagen",""), it.get("ubicacion",""),
                it.get("categoria","otro"), it.get("score",0),
                it.get("razon",""), it.get("mensaje_wa",""), ahora,
            ))
        except Exception as e:
            log.warning(f"Error guardando {it.get('id')}: {e}")
    con.commit()
    log.info(f"Guardadas/ignoradas {len(items)} oportunidades")

def leer_oportunidades(limite=100, offset=0) -> list[dict]:
    con = init_oportunidades_db()
    rows = con.execute("""
        SELECT id,fuente,tipo,titulo,precio,moneda,url,imagen,ubicacion,
               categoria,score,razon,mensaje_wa,estado,encontrado_en
        FROM oportunidades
        WHERE estado != 'descartado'
        ORDER BY score DESC, encontrado_en DESC
        LIMIT ? OFFSET ?
    """, (limite, offset)).fetchall()

    cols = ["id","fuente","tipo","titulo","precio","moneda","url","imagen","ubicacion",
            "categoria","score","razon","mensaje_wa","estado","encontrado_en"]
    return [dict(zip(cols, row)) for row in rows]

# ─── Ciclo de búsqueda ─────────────────────────────────────────────────────────

def run_busqueda():
    global _ultimo_run, _estado_run
    with _lock:
        if _estado_run == "corriendo":
            log.info("Ya hay una búsqueda en curso, saltando")
            return
        _estado_run = "corriendo"

    try:
        log.info("=== Iniciando ciclo de búsqueda ===")
        init_db()
        listados = buscar_todo()
        log.info(f"Fetcheados {len(listados)} listados nuevos")

        if listados:
            relevantes = analizar_todos(listados, solo_relevantes=True)
            log.info(f"Relevantes: {len(relevantes)}")
            if relevantes:
                guardar_oportunidades(relevantes)
        else:
            log.info("Sin listados nuevos en este ciclo")

        _ultimo_run = datetime.now(timezone.utc).isoformat()
        _estado_run = "ok"
        log.info("=== Ciclo completado ===")
    except Exception as e:
        log.error(f"Error en ciclo de búsqueda: {e}")
        _estado_run = "error"

# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return jsonify({
        "servicio": "Polo Antiguo Buscador",
        "version": "1.0.0",
        "estado": _estado_run,
        "ultimo_run": _ultimo_run,
        "endpoints": ["/api/oportunidades", "/api/status", "/api/trigger"],
    })

@app.route("/api/oportunidades")
def api_oportunidades():
    limite = int(request.args.get("limite", 50))
    offset = int(request.args.get("offset", 0))
    categoria = request.args.get("categoria", None)
    tipo = request.args.get("tipo", None)

    items = leer_oportunidades(limite=min(limite, 200), offset=offset)

    if categoria:
        items = [i for i in items if i["categoria"] == categoria]
    if tipo:
        items = [i for i in items if i["tipo"] == tipo]

    total = len(items)
    compradores = sum(1 for i in items if i["tipo"] == "comprador")
    vendedores = sum(1 for i in items if i["tipo"] == "vendedor")
    contactados = sum(1 for i in items if i["estado"] == "contactado")

    return jsonify({
        "ok": True,
        "stats": {
            "total": total,
            "compradores": compradores,
            "vendedores": vendedores,
            "contactados": contactados,
        },
        "ultimo_run": _ultimo_run,
        "estado_sistema": _estado_run,
        "oportunidades": items,
    })

@app.route("/api/status")
def api_status():
    return jsonify({
        "ok": True,
        "estado": _estado_run,
        "ultimo_run": _ultimo_run,
    })

@app.route("/api/trigger", methods=["POST"])
def api_trigger():
    secret = os.environ.get("TRIGGER_SECRET", "polo2024")
    if request.headers.get("X-API-KEY") != secret:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    if _estado_run == "corriendo":
        return jsonify({"ok": False, "error": "Ya hay una búsqueda en curso"})

    hilo = threading.Thread(target=run_busqueda, daemon=True)
    hilo.start()
    return jsonify({"ok": True, "mensaje": "Búsqueda iniciada"})

@app.route("/api/oportunidades/<lid>/estado", methods=["PUT"])
def api_actualizar_estado(lid):
    data = request.get_json() or {}
    nuevo_estado = data.get("estado")
    if nuevo_estado not in ("contactado", "descartado", "nuevo"):
        return jsonify({"ok": False, "error": "Estado inválido"}), 400

    con = init_oportunidades_db()
    con.execute("UPDATE oportunidades SET estado=? WHERE id=?", (nuevo_estado, lid))
    con.commit()
    return jsonify({"ok": True, "id": lid, "estado": nuevo_estado})

# ─── Cotizaciones proxy ────────────────────────────────────────────────────────

import requests as req_lib

@app.route("/api/cotizaciones")
def api_cotizaciones():
    """Proxy server-side para APIs de cotizaciones — sin problemas de CORS."""
    resultado = {}

    try:
        r = req_lib.get("https://dolarapi.com/v1/dolares", timeout=8)
        resultado["ars"] = r.json()
    except Exception as e:
        resultado["ars"] = None
        resultado["ars_error"] = str(e)

    try:
        r = req_lib.get("https://api.metals.live/v1/spot", timeout=8)
        resultado["metales"] = r.json()
    except Exception as e:
        resultado["metales"] = None
        resultado["metales_error"] = str(e)

    try:
        r = req_lib.get("https://api.frankfurter.app/latest?from=USD&to=GBP,BRL,CLP,UYU,JPY,CHF", timeout=8)
        resultado["fx"] = r.json()
    except Exception as e:
        try:
            r2 = req_lib.get("https://open.er-api.com/v6/latest/USD", timeout=8)
            resultado["fx"] = r2.json()
        except Exception as e2:
            resultado["fx"] = None
            resultado["fx_error"] = str(e2)

    return jsonify({"ok": True, **resultado})


# ─── Scheduler + búsqueda inicial (fuera de __main__ para funcionar con gunicorn) ───

### CAMBIO AQUÍ ### arranca al importar el módulo, no solo con python app.py
init_oportunidades_db()

scheduler = BackgroundScheduler(timezone="America/Argentina/Buenos_Aires")
scheduler.add_job(run_busqueda, "interval", hours=4, id="busqueda_periodica")
scheduler.start()

hilo_inicial = threading.Thread(target=run_busqueda, daemon=True)
hilo_inicial.start()

log.info("Scheduler y búsqueda inicial arrancados (compatible con gunicorn)")

# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    try:
        app.run(host="0.0.0.0", port=port, debug=False)
    finally:
        scheduler.shutdown()
