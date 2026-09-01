# main.py
# CronoAndes - Publicador/visor LIVE y RESULTADOS OFICIALES
# Basado en el proxy anterior, manteniendo descubrimiento por GitHub,
# polling y Socket.IO, pero usando la API de resultados calculados por CronoAndes.

from flask import Flask, jsonify, request, redirect, url_for
from flask_cors import CORS
from flask_socketio import SocketIO, join_room
import os
import requests
import logging
import json
import time
import threading
from datetime import datetime, timezone
from urllib.parse import quote
from pathlib import PurePosixPath

# ============================================================
# CONFIGURACIÓN
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "cronoandes-secure-key-2025",
)

allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").strip()
if allowed_origins == "*":
    cors_origins = "*"
else:
    cors_origins = [x.strip() for x in allowed_origins.split(",") if x.strip()]

CORS(app, resources={r"/*": {"origins": cors_origins}})

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="gevent",
    ping_interval=25,
    ping_timeout=60,
)

# ------------------------------------------------------------
# GitHub: descubrimiento del túnel CronoAndes
# ------------------------------------------------------------
GITHUB_CONFIG_URL = os.environ.get(
    "GITHUB_CONFIG_URL",
    "https://raw.githubusercontent.com/multhuaptff/crono-server-ciclismo/main/crono_server_url.json",
).strip()

# ------------------------------------------------------------
# GitHub: almacenamiento de snapshots finales.
# IMPORTANTE: por defecto usamos el repositorio separado
# crono-server-ciclismo para no provocar redeploy de crono-nube.
# ------------------------------------------------------------
RESULTS_GITHUB_TOKEN = os.environ.get("RESULTS_GITHUB_TOKEN", "").strip()
RESULTS_REPO_OWNER = os.environ.get("RESULTS_REPO_OWNER", "multhuaptff").strip()
RESULTS_REPO_NAME = os.environ.get("RESULTS_REPO_NAME", "crono-server-ciclismo").strip()
RESULTS_DIR = os.environ.get("RESULTS_DIR", "resultados_cronoandes").strip().strip("/")

# ------------------------------------------------------------
# Token para que solo CronoAndes publique resultados oficiales.
# ------------------------------------------------------------
PUBLIC_PUBLISH_TOKEN = os.environ.get(
    "PUBLIC_PUBLISH_TOKEN",
    "",
).strip()

# ============================================================
# ESTADO GLOBAL
# ============================================================
SERVER_URL = ""
server_url_updated = 0.0
SERVER_URL_TTL = 15.0

polling_interval = max(
    1,
    int(os.environ.get("POLLING_INTERVAL", "3")),
)

pollers = {}
pollers_lock = threading.Lock()

# ============================================================
# UTILIDADES
# ============================================================
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def safe_event_code(event_code: str) -> str:
    value = str(event_code or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    cleaned = "".join(ch if ch in allowed else "_" for ch in value)
    return cleaned[:120] or "evento"


# ============================================================
# DESCUBRIMIENTO DEL SERVIDOR LOCAL
# ============================================================
def get_server_url():
    """Obtiene la URL pública actual que CronoAndes publica en GitHub."""
    try:
        response = requests.get(
            GITHUB_CONFIG_URL,
            timeout=8,
            headers={"Cache-Control": "no-cache"},
        )
        response.raise_for_status()
        data = response.json()

        url = data.get("url_publica")
        if url:
            return str(url).rstrip("/")

        urls = data.get("urls") or []
        if urls:
            return str(urls[0]).rstrip("/")

    except Exception as exc:
        logging.warning(
            "No se pudo resolver URL desde GitHub: %s",
            exc,
        )

    return os.environ.get("SERVER_URL", "").strip().rstrip("/")


def resolve_server_url(force=False):
    global SERVER_URL, server_url_updated

    if (
        not force
        and SERVER_URL
        and (time.time() - server_url_updated) < SERVER_URL_TTL
    ):
        return SERVER_URL

    resolved = get_server_url()
    if resolved and resolved != SERVER_URL:
        logging.info(
            "🔗 URL CronoAndes actualizada: %s",
            resolved,
        )

    SERVER_URL = resolved
    server_url_updated = time.time()
    return SERVER_URL


# ============================================================
# API HACIA CRONOANDES
# ============================================================
def fetch_public_results(event_code):
    server_url = resolve_server_url()
    if not server_url:
        return None

    url = (
        f"{server_url}/api/public/resultados/"
        f"{quote(event_code, safe='') }"
    )

    try:
        response = requests.get(
            url,
            timeout=8,
            headers={"Accept": "application/json"},
        )

        if response.status_code == 200:
            return response.json()

        # Si el túnel cambió, intentar resolverlo inmediatamente una vez.
        if response.status_code in (404, 409, 502, 503, 504):
            refreshed = resolve_server_url(force=True)
            if refreshed and refreshed != server_url:
                retry_url = (
                    f"{refreshed}/api/public/resultados/"
                    f"{quote(event_code, safe='')}"
                )
                retry = requests.get(
                    retry_url,
                    timeout=8,
                    headers={"Accept": "application/json"},
                )
                if retry.status_code == 200:
                    return retry.json()

        logging.warning(
            "GET resultados event_code=%s status=%s",
            event_code,
            response.status_code,
        )
        return None

    except requests.RequestException as exc:
        logging.warning(
            "Error consultando CronoAndes %s: %s",
            event_code,
            exc,
        )
        return None


# ============================================================
# SNAPSHOT FINAL EN GITHUB
# ============================================================
def github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if RESULTS_GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {RESULTS_GITHUB_TOKEN}"
    return headers


def github_result_path(event_code):
    filename = f"{safe_event_code(event_code)}.json"
    return str(PurePosixPath(RESULTS_DIR) / filename)


def save_final_snapshot(event_code, payload):
    """
    Crea/actualiza el snapshot oficial en el repositorio de resultados.
    No depende del filesystem de Render.
    """
    if not RESULTS_GITHUB_TOKEN:
        return False, "RESULTS_GITHUB_TOKEN no configurado."

    path = github_result_path(event_code)
    api_url = (
        f"https://api.github.com/repos/"
        f"{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}/contents/"
        f"{quote(path, safe='/')}"
    )

    document = json.dumps(
        json_safe(payload),
        ensure_ascii=False,
        indent=2,
    )
    content_b64 = __import__("base64").b64encode(
        document.encode("utf-8")
    ).decode("ascii")

    headers = github_headers()

    sha = None
    try:
        existing = requests.get(
            api_url,
            headers=headers,
            timeout=10,
        )
        if existing.status_code == 200:
            sha = existing.json().get("sha")
        elif existing.status_code != 404:
            return False, f"GitHub GET devolvió {existing.status_code}."
    except requests.RequestException as exc:
        return False, f"Error consultando GitHub: {exc}"

    body = {
        "message": f"Publicar resultado oficial {event_code}",
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        body["sha"] = sha

    try:
        response = requests.put(
            api_url,
            headers=headers,
            json=body,
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, f"Error escribiendo en GitHub: {exc}"

    if response.status_code not in (200, 201):
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:500]
        return False, f"GitHub rechazó la publicación: {detail}"

    raw_url = (
        f"https://raw.githubusercontent.com/"
        f"{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}/main/{path}"
    )

    return True, {
        "path": path,
        "raw_url": raw_url,
        "published_at": now_iso(),
    }


def load_final_snapshot(event_code):
    path = github_result_path(event_code)
    raw_url = (
        f"https://raw.githubusercontent.com/"
        f"{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}/main/{path}"
    )
    try:
        response = requests.get(
            raw_url,
            timeout=8,
            headers={"Cache-Control": "no-cache"},
        )
        if response.status_code == 200:
            return response.json()
    except requests.RequestException as exc:
        logging.warning(
            "Error leyendo snapshot final %s: %s",
            event_code,
            exc,
        )
    return None


# ============================================================
# POLLING + SOCKET.IO
# ============================================================
def start_polling(event_code):
    event_code = str(event_code or "").strip()
    if not event_code:
        return

    with pollers_lock:
        if event_code in pollers and pollers[event_code].get("active"):
            return

        state = {"active": True}
        pollers[event_code] = state

    logging.info(
        "▶️ Polling iniciado para %s cada %ss",
        event_code,
        polling_interval,
    )

    def poll():
        last_signature = None

        while state["active"]:
            try:
                payload = fetch_public_results(event_code)

                if payload:
                    # JSON canónico para detectar cambios sin depender del orden.
                    signature = json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )

                    if signature != last_signature:
                        last_signature = signature
                        socketio.emit(
                            "public_resultados",
                            payload,
                            room=event_code,
                        )

                        # Compatibilidad con el visor anterior.
                        socketio.emit(
                            "nuevo_tiempo",
                            payload.get("resultados", []),
                            room=event_code,
                        )

                socketio.sleep(polling_interval)

            except Exception as exc:
                logging.error(
                    "Error polling %s: %s",
                    event_code,
                    exc,
                    exc_info=True,
                )
                socketio.sleep(polling_interval)

        logging.info(
            "⏹️ Polling detenido para %s",
            event_code,
        )

    thread = threading.Thread(
        target=poll,
        name=f"public-poll-{safe_event_code(event_code)}",
        daemon=True,
    )
    state["thread"] = thread
    thread.start()


@socketio.on("subscribe")
def on_subscribe(data):
    data = data or {}
    event_code = str(data.get("event_code", "")).strip()

    if not event_code:
        return

    join_room(event_code)
    logging.info(
        "👀 Cliente suscrito a evento: %s",
        event_code,
    )
    start_polling(event_code)


# ============================================================
# API PÚBLICA
# ============================================================
@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "app": "CronoAndes Public Results",
        "server_url": resolve_server_url(),
        "polling_interval": polling_interval,
        "result_repository": (
            f"{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}"
        ),
        "server_time": now_iso(),
    })


@app.get("/api/status")
def status():
    with pollers_lock:
        active_events = sorted(
            code for code, state in pollers.items()
            if state.get("active")
        )

    return jsonify({
        "status": "ok",
        "server_url": resolve_server_url(),
        "polling_interval": polling_interval,
        "polling_events": active_events,
        "results_repo": f"{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}",
    })


@app.get("/api/public/live/<event_code>")
def api_public_live(event_code):
    payload = fetch_public_results(event_code)
    if not payload:
        return jsonify({
            "status": "offline",
            "event_code": event_code,
            "message": "CronoAndes no está disponible o el evento no está activo.",
            "server_url": resolve_server_url(),
        }), 503
    return jsonify(payload)


@app.get("/api/public/final/<event_code>")
def api_public_final(event_code):
    payload = load_final_snapshot(event_code)
    if not payload:
        return jsonify({
            "status": "not_found",
            "event_code": event_code,
            "message": "No existe un resultado oficial publicado.",
        }), 404
    return jsonify(payload)


@app.get("/api/inscritos/<event_code>")
def compatibility_inscritos(event_code):
    payload = fetch_public_results(event_code)
    if not payload:
        return jsonify([])
    return jsonify([
        {
            "participante_id": r.get("participante_id"),
            "dorsal": r.get("dorsal"),
            "nombre": r.get("nombre"),
            "categoria": r.get("categoria"),
            "club": r.get("club"),
        }
        for r in payload.get("resultados", [])
    ])


@app.get("/api/tiempos/<event_code>")
def compatibility_tiempos(event_code):
    payload = fetch_public_results(event_code)
    if not payload:
        return jsonify([])

    tiempos = []
    for r in payload.get("resultados", []):
        if r.get("salida"):
            tiempos.append({
                "dorsal": r.get("dorsal"),
                "nombre": r.get("nombre"),
                "categoria": r.get("categoria"),
                "action": "salida",
                "timestamp": r.get("salida"),
            })
        if r.get("llegada"):
            tiempos.append({
                "dorsal": r.get("dorsal"),
                "nombre": r.get("nombre"),
                "categoria": r.get("categoria"),
                "action": "llegada",
                "timestamp": r.get("llegada"),
            })
    return jsonify(tiempos)


@app.get("/api/refresh/<event_code>")
def refresh(event_code):
    payload = fetch_public_results(event_code)
    if payload:
        socketio.emit(
            "public_resultados",
            payload,
            room=event_code,
        )
        socketio.emit(
            "nuevo_tiempo",
            payload.get("resultados", []),
            room=event_code,
        )
    return jsonify({
        "status": "ok" if payload else "offline",
        "event_code": event_code,
        "count": len(payload.get("resultados", [])) if payload else 0,
    })


@app.post("/api/public/finalizar/<event_code>")
def public_finalizar(event_code):
    """Recibe el snapshot desde CronoAndes y lo deja como oficial."""
    if not PUBLIC_PUBLISH_TOKEN:
        return jsonify({
            "status": "error",
            "error": "PUBLIC_PUBLISH_TOKEN no configurado en crono-nube.",
        }), 503

    supplied = request.headers.get(
        "X-CronoAndes-Publish-Token",
        "",
    ).strip()

    if supplied != PUBLIC_PUBLISH_TOKEN:
        return jsonify({
            "status": "error",
            "error": "Token de publicación inválido.",
        }), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            "status": "error",
            "error": "JSON de publicación inválido.",
        }), 400

    if str(payload.get("event_code", "")).strip() != str(event_code).strip():
        return jsonify({
            "status": "error",
            "error": "event_code inconsistente.",
        }), 400

    payload["status"] = "final"
    payload["tipo_publicacion"] = "oficial"
    payload["publicado_en"] = payload.get("publicado_en") or now_iso()

    ok, detail = save_final_snapshot(
        event_code,
        payload,
    )

    if not ok:
        logging.error(
            "❌ No se pudo guardar resultado oficial %s: %s",
            event_code,
            detail,
        )
        return jsonify({
            "status": "error",
            "error": detail,
        }), 502

    logging.info(
        "🏁 Resultado oficial guardado: %s",
        event_code,
    )

    return jsonify({
        "status": "ok",
        "event_code": event_code,
        "resultado": detail,
        "live_url": f"/live/{quote(event_code, safe='')}",
        "final_url": f"/resultados/{quote(event_code, safe='')}",
    })


# ============================================================
# VISOR WEB
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CronoAndes — Resultados</title>
    <style>
        :root {
            --bg: #f4f7fb;
            --panel: #ffffff;
            --text: #0f172a;
            --muted: #64748b;
            --line: #dbe3ef;
            --header: #102a6b;
            --header2: #173c96;
            --accent: #2563eb;
            --success: #15803d;
            --warning: #b45309;
            --danger: #b91c1c;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Inter, Segoe UI, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
        }
        .top {
            background: linear-gradient(135deg, var(--header), var(--header2));
            color: white;
            padding: 20px 16px;
            position: sticky;
            top: 0;
            z-index: 10;
            box-shadow: 0 3px 12px rgba(0,0,0,.18);
        }
        .top-inner {
            max-width: 1500px;
            margin: 0 auto;
            display: flex;
            justify-content: space-between;
            gap: 16px;
            align-items: center;
            flex-wrap: wrap;
        }
        h1 { margin: 0; font-size: 1.6rem; }
        .sub { opacity: .88; margin-top: 5px; font-size: .92rem; }
        .status {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(255,255,255,.14);
            font-weight: 700;
        }
        .dot {
            width: 10px; height: 10px; border-radius: 50%;
            background: #22c55e;
            box-shadow: 0 0 0 4px rgba(34,197,94,.18);
        }
        .dot.offline { background: #ef4444; box-shadow: 0 0 0 4px rgba(239,68,68,.16); }
        main { max-width: 1500px; margin: 20px auto; padding: 0 14px 40px; }
        .toolbar {
            display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 14px;
        }
        .toolbar select, .toolbar input {
            border: 1px solid var(--line); border-radius: 8px; padding: 9px 11px;
            background: white; color: var(--text);
        }
        .toolbar .hint { color: var(--muted); font-size: .88rem; }
        .panel {
            background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
            overflow: hidden; box-shadow: 0 5px 18px rgba(15,23,42,.05);
            margin-bottom: 18px;
        }
        .panel-title {
            padding: 13px 15px; background: #eef4ff; color: var(--header);
            font-size: 1.05rem; font-weight: 800; border-bottom: 1px solid var(--line);
        }
        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; min-width: 980px; }
        th, td { padding: 10px 9px; border-bottom: 1px solid #edf1f7; text-align: center; white-space: nowrap; }
        th { background: #f8fafc; color: #475569; font-size: .82rem; text-transform: uppercase; letter-spacing: .03em; }
        td.name { text-align: left; min-width: 240px; font-weight: 700; }
        tr.final td:first-child { font-weight: 900; color: var(--header); }
        tr.inprogress { background: #fbfdff; }
        .state-final { color: var(--success); font-weight: 800; }
        .state-race { color: var(--accent); font-weight: 800; }
        .state-dnf { color: var(--warning); font-weight: 800; }
        .state-dns { color: var(--muted); font-weight: 700; }
        .progress { font-weight: 800; }
        .muted { color: var(--muted); }
        .empty { padding: 40px 20px; text-align: center; color: var(--muted); }
        .official {
            border-left: 5px solid var(--success);
            padding: 12px 15px;
            background: #f0fdf4;
            color: #166534;
            margin-bottom: 16px;
            border-radius: 8px;
            display: none;
        }
        .offline-box {
            display: none; padding: 18px; border-radius: 10px; background: #fff7ed;
            border: 1px solid #fed7aa; color: #9a3412; margin-bottom: 18px;
        }
        footer { text-align: center; color: var(--muted); font-size: .8rem; margin-top: 22px; }
        @media (max-width: 700px) {
            h1 { font-size: 1.25rem; }
            .top { padding: 15px 12px; }
            main { padding: 0 9px 30px; }
        }
    </style>
</head>
<body>
<div class="top">
    <div class="top-inner">
        <div>
            <h1>🏆 CronoAndes — Resultados</h1>
            <div class="sub" id="event-info">Cargando evento...</div>
        </div>
        <div class="status"><span id="status-dot" class="dot"></span><span id="status-text">CARGANDO</span></div>
    </div>
</div>

<main>
    <div id="official-box" class="official">🏁 RESULTADOS OFICIALES PUBLICADOS</div>
    <div id="offline-box" class="offline-box">🔴 CronoAndes no está transmitiendo resultados en este momento. La página volverá a actualizarse cuando el sistema esté disponible.</div>

    <div class="toolbar">
        <input id="search" type="search" placeholder="Buscar dorsal o nombre...">
        <select id="category"><option value="">Todas las categorías</option></select>
        <span class="hint" id="updated">Última actualización: —</span>
    </div>

    <div class="panel">
        <div class="panel-title">Clasificación</div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Pos.</th>
                        <th>Dorsal</th>
                        <th>Nombre</th>
                        <th>Categoría</th>
                        <th>Vueltas</th>
                        <th>Estado</th>
                        <th>Tiempo Total</th>
                        <th>Dif. General</th>
                        <th>Dif. Categoría</th>
                    </tr>
                </thead>
                <tbody id="tbody"></tbody>
            </table>
        </div>
        <div id="empty" class="empty">Esperando resultados...</div>
    </div>

    <footer>CronoAndes · Resultados en vivo y oficiales</footer>
</main>

<script src="https://cdn.socket.io/4.7.4/socket.io.min.js"></script>
<script>
(function() {
    const params = new URLSearchParams(window.location.search);
    const pathParts = window.location.pathname.split('/').filter(Boolean);
    let eventCode = params.get('event_code') || '';
    let mode = pathParts[0] === 'resultados' ? 'final' : 'live';
    if (!eventCode && pathParts.length >= 2 && (pathParts[0] === 'live' || pathParts[0] === 'resultados')) {
        eventCode = decodeURIComponent(pathParts[1]);
    }

    const tbody = document.getElementById('tbody');
    const empty = document.getElementById('empty');
    const statusText = document.getElementById('status-text');
    const dot = document.getElementById('status-dot');
    const eventInfo = document.getElementById('event-info');
    const updated = document.getElementById('updated');
    const search = document.getElementById('search');
    const category = document.getElementById('category');
    const officialBox = document.getElementById('official-box');
    const offlineBox = document.getElementById('offline-box');

    let payload = null;

    function fmtSeconds(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
        const total = Math.max(0, Number(value));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = Math.floor(total % 60);
        const ms = Math.floor((total - Math.floor(total)) * 1000);
        if (h > 0) return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`;
        return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`;
    }

    function fmtDiff(value) {
        if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
        const n = Number(value);
        return n <= 0.000001 ? 'LÍDER' : `+${fmtSeconds(n)}`;
    }

    function stateClass(state) {
        if (state === 'Finalizado') return 'state-final';
        if (state === 'En curso') return 'state-race';
        if (state === 'DNF') return 'state-dnf';
        return 'state-dns';
    }

    function updateCategories(rows) {
        const current = category.value;
        const cats = [...new Set(rows.map(r => r.categoria || 'SIN CATEGORÍA'))].sort();
        category.innerHTML = '<option value="">Todas las categorías</option>';
        cats.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c; opt.textContent = c; category.appendChild(opt);
        });
        if (cats.includes(current)) category.value = current;
    }

    function render() {
        if (!payload) {
            tbody.innerHTML = '';
            empty.style.display = 'block';
            return;
        }

        const rows = payload.resultados || [];
        updateCategories(rows);

        const q = search.value.trim().toLowerCase();
        const cat = category.value;
        const filtered = rows.filter(r => {
            const matchesText = !q || String(r.dorsal || '').toLowerCase().includes(q) || String(r.nombre || '').toLowerCase().includes(q);
            const matchesCat = !cat || String(r.categoria || '') === cat;
            return matchesText && matchesCat;
        });

        tbody.innerHTML = '';
        filtered.forEach(r => {
            const tr = document.createElement('tr');
            if (r.estado === 'Finalizado') tr.className = 'final';
            else if (r.estado === 'En curso') tr.className = 'inprogress';

            const pos = r.puesto_general ?? '—';
            const vueltasTotales = Number(r.vueltas_totales || 0);
            const vueltasHechas = Number(r.vueltas_completadas || 0);
            const vueltas = vueltasTotales > 0 ? `${vueltasHechas}/${vueltasTotales}` : `${vueltasHechas}`;

            tr.innerHTML = `
                <td><strong>${pos}</strong></td>
                <td><strong>${escapeHtml(r.dorsal || '')}</strong></td>
                <td class="name">${escapeHtml(r.nombre || '')}</td>
                <td>${escapeHtml(r.categoria || '')}</td>
                <td class="progress">${vueltas}</td>
                <td class="${stateClass(r.estado)}">${escapeHtml(r.estado || '')}</td>
                <td>${fmtSeconds(r.tiempo_total_seg)}</td>
                <td>${fmtDiff(r.diferencia_general_seg)}</td>
                <td>${fmtDiff(r.diferencia_categoria_seg)}</td>
            `;
            tbody.appendChild(tr);
        });

        empty.style.display = filtered.length ? 'none' : 'block';
        empty.textContent = rows.length ? 'No hay corredores que coincidan con el filtro.' : 'Esperando resultados...';

        const state = payload.estado_evento || '';
        const official = payload.status === 'final' || mode === 'final';
        officialBox.style.display = official ? 'block' : 'none';
        offlineBox.style.display = state === 'en_vivo' || official ? 'none' : 'block';

        if (official || state === 'en_vivo') {
            dot.classList.remove('offline');
            statusText.textContent = official ? 'RESULTADOS OFICIALES' : 'EN VIVO';
        } else {
            dot.classList.add('offline');
            statusText.textContent = 'SIN CONEXIÓN';
        }

        eventInfo.textContent = `Evento ${payload.event_code || eventCode} · Etapa ${payload.etapa_id ?? '—'} · ${payload.modalidad || ''}`;
        updated.textContent = `Última actualización: ${payload.actualizado_en || '—'}`;
    }

    function escapeHtml(value) {
        return String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
    }

    async function loadInitial() {
        if (!eventCode) {
            empty.textContent = 'Falta el código del evento.';
            return;
        }

        try {
            let response;
            if (mode === 'final') {
                response = await fetch(`/api/public/final/${encodeURIComponent(eventCode)}`, {cache:'no-store'});
            } else {
                response = await fetch(`/api/public/live/${encodeURIComponent(eventCode)}`, {cache:'no-store'});
            }
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            payload = await response.json();
            render();
        } catch (err) {
            console.error(err);
            dot.classList.add('offline');
            statusText.textContent = 'SIN CONEXIÓN';
            offlineBox.style.display = mode === 'live' ? 'block' : 'none';
            empty.textContent = mode === 'live' ? 'Esperando conexión con CronoAndes...' : 'No existe un resultado oficial publicado.';
            empty.style.display = 'block';
        }
    }

    search.addEventListener('input', render);
    category.addEventListener('change', render);

    loadInitial();

    const socket = io(window.location.origin, {
        transports: ['websocket', 'polling'],
        reconnection: true,
        reconnectionAttempts: Infinity,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
    });

    socket.on('connect', () => {
        if (mode === 'live' && eventCode) {
            socket.emit('subscribe', {event_code: eventCode});
        }
    });

    socket.on('public_resultados', data => {
        if (mode !== 'live') return;
        payload = data;
        render();
    });
})();
</script>
</body>
</html>"""

@app.get("/")
def home():
    return redirect(url_for("live_page_default"))

@app.get("/live")
def live_page_default():
    return HTML_PAGE

@app.get("/live/<event_code>")
def live_page(event_code):
    # La página obtiene el event_code del path mediante JS.
    return HTML_PAGE

@app.get("/pantalla")
def pantalla_compat():
    return HTML_PAGE

@app.get("/resultados/<event_code>")
def final_page(event_code):
    return HTML_PAGE


# ============================================================
# ARRANQUE
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "0.0.0.0")

    logging.info("🚀 CronoAndes Public Results arrancando en %s:%s", host, port)
    logging.info("📡 URL CronoAndes: %s", resolve_server_url(force=True) or "NO DETECTADA")
    logging.info(
        "🗃️ Snapshots: %s/%s/%s",
        RESULTS_REPO_OWNER,
        RESULTS_REPO_NAME,
        RESULTS_DIR,
    )

    socketio.run(
        app,
        host=host,
        port=port,
        allow_unsafe_werkzeug=False,
    )
