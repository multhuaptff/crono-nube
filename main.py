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
import re
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
# REGISTRO PÚBLICO DE EVENTOS
# ============================================================
PUBLIC_EVENTS_GITHUB_PATH = os.environ.get("PUBLIC_EVENTS_GITHUB_PATH", "eventos_cronoandes.json").strip().strip("/")
public_events = {}
public_events_lock = threading.Lock()
PUBLIC_EVENT_STALE_SECONDS = max(30, int(os.environ.get("PUBLIC_EVENT_STALE_SECONDS", "45")))


def slugify_event_name(name):
    import unicodedata
    value = str(name or "evento").strip().lower()
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:80] or "evento"


def _github_events_api_url():
    return f"https://api.github.com/repos/{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}/contents/{quote(PUBLIC_EVENTS_GITHUB_PATH, safe='/')}"


def _github_events_raw_url():
    return f"https://raw.githubusercontent.com/{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}/main/{PUBLIC_EVENTS_GITHUB_PATH}"


def load_public_events():
    loaded = None
    if RESULTS_GITHUB_TOKEN:
        try:
            response = requests.get(_github_events_api_url(), headers=github_headers(), timeout=8)
            if response.status_code == 200:
                body = response.json()
                content = body.get("content")
                if content:
                    import base64
                    loaded = json.loads(base64.b64decode(content).decode("utf-8"))
        except Exception as exc:
            logging.warning("No se pudo cargar catálogo público por API GitHub: %s", exc)
    if loaded is None:
        try:
            response = requests.get(_github_events_raw_url(), timeout=8, headers={"Cache-Control": "no-cache"})
            if response.status_code == 200:
                loaded = response.json()
        except Exception as exc:
            logging.warning("No se pudo cargar catálogo público por raw GitHub: %s", exc)
    if not isinstance(loaded, dict):
        loaded = {}
    with public_events_lock:
        public_events.clear()
        for slug, item in loaded.items():
            if isinstance(item, dict) and item.get("event_code"):
                public_events[str(slug)] = dict(item)
    logging.info("📚 Eventos públicos cargados: %s", len(public_events))


def save_public_events():
    if not RESULTS_GITHUB_TOKEN:
        return False, "RESULTS_GITHUB_TOKEN no configurado."
    with public_events_lock:
        document = json.dumps(public_events, ensure_ascii=False, indent=2)
    import base64
    content_b64 = base64.b64encode(document.encode("utf-8")).decode("ascii")
    api_url = _github_events_api_url()
    headers = github_headers()
    sha = None
    try:
        existing = requests.get(api_url, headers=headers, timeout=8)
        if existing.status_code == 200:
            sha = existing.json().get("sha")
        elif existing.status_code != 404:
            return False, f"GitHub GET catálogo devolvió {existing.status_code}."
    except requests.RequestException as exc:
        return False, f"Error consultando catálogo GitHub: {exc}"
    body = {"message": "Actualizar catálogo público CronoAndes", "content": content_b64, "branch": "main"}
    if sha:
        body["sha"] = sha
    try:
        response = requests.put(api_url, headers=headers, json=body, timeout=15)
    except requests.RequestException as exc:
        return False, f"Error escribiendo catálogo GitHub: {exc}"
    if response.status_code not in (200, 201):
        return False, f"GitHub rechazó catálogo: {response.text[:500]}"
    return True, "ok"


def _public_event_view(item):
    slug = str(item.get("slug") or "")
    return {
        "slug": slug,
        "nombre": item.get("nombre") or "Evento CronoAndes",
        "etapa": item.get("etapa") or "",
        "modalidad": item.get("modalidad") or "",
        "estado": item.get("estado") or "offline",
        "actualizado_en": item.get("actualizado_en"),
        "live_url": f"/live/{quote(slug, safe='')}",
        "resultados_url": f"/resultados/{quote(slug, safe='')}",
    }


def get_public_event(slug):
    with public_events_lock:
        item = public_events.get(slug)
        return dict(item) if item else None


def resolve_public_slug_for_event(event_code):
    with public_events_lock:
        for slug, item in public_events.items():
            if str(item.get("event_code")) == str(event_code):
                return slug
    return None


def register_public_event(data):
    event_code = str(data.get("event_code") or "").strip()
    nombre = str(data.get("nombre") or "").strip()
    if not event_code or not nombre:
        return None, "event_code y nombre son obligatorios."
    requested_slug = slugify_event_name(data.get("slug") or nombre)
    now = now_iso()
    with public_events_lock:
        existing_slug = None
        for s, it in public_events.items():
            if str(it.get("event_code")) == event_code:
                existing_slug = s
                break
        if existing_slug:
            slug = existing_slug
        else:
            slug = requested_slug
            idx = 2
            while slug in public_events:
                slug = f"{requested_slug}-{idx}"
                idx += 1
        public_events[slug] = {
            "slug": slug,
            "event_code": event_code,
            "nombre": nombre,
            "etapa": str(data.get("etapa") or "").strip(),
            "etapa_id": data.get("etapa_id"),
            "modalidad": str(data.get("modalidad") or "").strip().lower(),
            "estado": str(data.get("estado") or "en_vivo").strip(),
            "actualizado_en": now,
            "server_url": str(data.get("server_url") or "").strip().rstrip("/"),
        }
        item = dict(public_events[slug])
    ok, detail = save_public_events()
    if not ok:
        logging.warning("⚠️ Catálogo público no persistido: %s", detail)
    return item, None


def prune_stale_events():
    cutoff = time.time() - PUBLIC_EVENT_STALE_SECONDS
    changed = False
    with public_events_lock:
        for item in public_events.values():
            if item.get("estado") != "en_vivo":
                continue
            raw = item.get("actualizado_en")
            try:
                ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0
            if ts < cutoff:
                item["estado"] = "offline"
                changed = True
    if changed:
        save_public_events()


def _public_payload_for_client(payload):
    data = dict(payload or {})
    event_code = data.pop("event_code", None)
    slug = resolve_public_slug_for_event(event_code) if event_code else None
    item = get_public_event(slug) if slug else None
    if item:
        data["nombre_evento"] = item.get("nombre")
        data["slug"] = item.get("slug")
        data["etapa_nombre"] = item.get("etapa")
        data["evento"] = _public_event_view(item)
    return data


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
    logging.info("▶️ Polling iniciado para %s cada %ss", event_code, polling_interval)
    def poll():
        last_signature = None
        while state["active"]:
            try:
                payload = fetch_public_results(event_code)
                if payload:
                    signature = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if signature != last_signature:
                        last_signature = signature
                        socketio.emit("public_resultados", _public_payload_for_client(payload), room=event_code)
                        socketio.emit("nuevo_tiempo", payload.get("resultados", []), room=event_code)
                socketio.sleep(polling_interval)
            except Exception as exc:
                logging.error("Error polling %s: %s", event_code, exc, exc_info=True)
                socketio.sleep(polling_interval)
    thread = threading.Thread(target=poll, name=f"public-poll-{safe_event_code(event_code)}", daemon=True)
    state["thread"] = thread
    thread.start()


@socketio.on("subscribe")
def on_subscribe(data):
    data = data or {}
    slug = str(data.get("slug") or "").strip().strip("/")
    item = get_public_event(slug)
    if not item:
        return
    event_code = str(item.get("event_code") or "").strip()
    if not event_code:
        return
    join_room(event_code)
    logging.info("👀 Cliente suscrito a evento público: %s", slug)
    start_polling(event_code)


# ============================================================
# API PÚBLICA
# ============================================================
@app.get("/health")
def health():
    prune_stale_events()
    return jsonify({"status":"ok","app":"CronoAndes Public Results","server_url":resolve_server_url(),"polling_interval":polling_interval,"result_repository":f"{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}","server_time":now_iso()})


@app.get("/api/status")
def status():
    prune_stale_events()
    with pollers_lock:
        active_events = sorted(code for code,state in pollers.items() if state.get("active"))
    with public_events_lock:
        catalog_count = len(public_events)
    return jsonify({"status":"ok","server_url":resolve_server_url(),"polling_interval":polling_interval,"polling_events":active_events,"public_events":catalog_count,"results_repo":f"{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}"})


@app.get("/api/public/eventos")
def api_public_eventos():
    prune_stale_events()
    with public_events_lock:
        items = [_public_event_view(item) for item in public_events.values()]
    items.sort(key=lambda x:(x.get("estado") != "en_vivo", str(x.get("nombre") or "").lower()))
    return jsonify({"status":"ok","eventos":items,"server_time":now_iso()})


@app.get("/api/public/eventos/<slug>")
def api_public_evento(slug):
    item = get_public_event(slug)
    if not item:
        return jsonify({"status":"not_found","message":"Evento público no encontrado."}),404
    return jsonify({"status":"ok","evento":_public_event_view(item)})


@app.post("/api/public/registrar-evento")
def api_public_registrar_evento():
    supplied = request.headers.get("X-CronoAndes-Publish-Token", "").strip()
    if not PUBLIC_PUBLISH_TOKEN or supplied != PUBLIC_PUBLISH_TOKEN:
        return jsonify({"status":"error","error":"Token de publicación inválido."}),401
    data = request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify({"status":"error","error":"JSON inválido."}),400
    item,error = register_public_event(data)
    if error:
        return jsonify({"status":"error","error":error}),400
    return jsonify({"status":"ok","evento":_public_event_view(item),"server_time":now_iso()})


@app.get("/api/public/live-event/<slug>")
def api_public_live_event(slug):
    item = get_public_event(slug)
    if not item:
        return jsonify({"status":"not_found","message":"Evento público no encontrado."}),404
    payload = fetch_public_results(str(item.get("event_code") or ""))
    if not payload:
        return jsonify({"status":"offline","evento":_public_event_view(item),"message":"CronoAndes no está transmitiendo resultados en este momento."}),503
    public_payload = _public_payload_for_client(payload)
    public_payload["status"] = "ok"
    public_payload["evento"] = _public_event_view(item)
    return jsonify(public_payload)


@app.get("/api/public/final-event/<slug>")
def api_public_final_event(slug):
    item = get_public_event(slug)
    if not item:
        return jsonify({"status":"not_found","message":"Evento público no encontrado."}),404
    payload = load_final_snapshot(str(item.get("event_code") or ""))
    if not payload:
        return jsonify({"status":"not_found","evento":_public_event_view(item),"message":"No existe un resultado oficial publicado."}),404
    public_payload = _public_payload_for_client(payload)
    public_payload["status"] = "final"
    public_payload["evento"] = _public_event_view(item)
    return jsonify(public_payload)


# Compatibilidad operativa: siguen existiendo las rutas basadas en event_code.
@app.get("/api/public/live/<event_code>")
def api_public_live(event_code):
    payload = fetch_public_results(event_code)
    if not payload:
        return jsonify({"status":"offline","message":"CronoAndes no está disponible o el evento no está activo."}),503
    return jsonify(_public_payload_for_client(payload))


@app.get("/api/public/final/<event_code>")
def api_public_final(event_code):
    payload = load_final_snapshot(event_code)
    if not payload:
        return jsonify({"status":"not_found","message":"No existe un resultado oficial publicado."}),404
    return jsonify(_public_payload_for_client(payload))


@app.get("/api/inscritos/<event_code>")
def compatibility_inscritos(event_code):
    payload = fetch_public_results(event_code)
    if not payload:
        return jsonify([])
    return jsonify([{"participante_id":r.get("participante_id"),"dorsal":r.get("dorsal"),"nombre":r.get("nombre"),"categoria":r.get("categoria"),"club":r.get("club")} for r in payload.get("resultados",[])])


@app.get("/api/tiempos/<event_code>")
def compatibility_tiempos(event_code):
    payload = fetch_public_results(event_code)
    if not payload:
        return jsonify([])
    tiempos=[]
    for r in payload.get("resultados",[]):
        if r.get("salida"):
            tiempos.append({"dorsal":r.get("dorsal"),"nombre":r.get("nombre"),"categoria":r.get("categoria"),"action":"salida","timestamp":r.get("salida")})
        if r.get("llegada"):
            tiempos.append({"dorsal":r.get("dorsal"),"nombre":r.get("nombre"),"categoria":r.get("categoria"),"action":"llegada","timestamp":r.get("llegada")})
    return jsonify(tiempos)


@app.get("/api/refresh/<event_code>")
def refresh(event_code):
    payload=fetch_public_results(event_code)
    if payload:
        socketio.emit("public_resultados",_public_payload_for_client(payload),room=event_code)
        socketio.emit("nuevo_tiempo",payload.get("resultados",[]),room=event_code)
    return jsonify({"status":"ok" if payload else "offline","count":len(payload.get("resultados",[])) if payload else 0})


@app.post("/api/public/finalizar/<event_code>")
def public_finalizar(event_code):
    if not PUBLIC_PUBLISH_TOKEN:
        return jsonify({"status":"error","error":"PUBLIC_PUBLISH_TOKEN no configurado en crono-nube."}),503
    supplied=request.headers.get("X-CronoAndes-Publish-Token","").strip()
    if supplied!=PUBLIC_PUBLISH_TOKEN:
        return jsonify({"status":"error","error":"Token de publicación inválido."}),401
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict):
        return jsonify({"status":"error","error":"JSON de publicación inválido."}),400
    if str(payload.get("event_code","")).strip()!=str(event_code).strip():
        return jsonify({"status":"error","error":"event_code inconsistente."}),400
    payload["status"]="final"
    payload["tipo_publicacion"]="oficial"
    payload["publicado_en"]=payload.get("publicado_en") or now_iso()
    ok,detail=save_final_snapshot(event_code,payload)
    if not ok:
        return jsonify({"status":"error","error":detail}),502
    slug=resolve_public_slug_for_event(event_code)
    if slug:
        with public_events_lock:
            public_events[slug]["estado"]="finalizado"
            public_events[slug]["actualizado_en"]=now_iso()
        save_public_events()
    return jsonify({"status":"ok","publicacion":detail,"live_url":f"/live/{quote(slug,safe='')}" if slug else None,"final_url":f"/resultados/{quote(slug,safe='')}" if slug else None})


# ============================================================
# VISOR WEB
# ============================================================
def build_live_html(mode, slug):
    safe_slug = json.dumps(str(slug or ""), ensure_ascii=False)
    safe_mode = json.dumps("final" if mode == "final" else "live")
    return """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>CronoAndes — Resultados</title>
<style>body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:#f4f7fb;color:#0f172a}.top{background:linear-gradient(135deg,#102a6b,#173c96);color:#fff;padding:20px 16px}.top-inner{max-width:1500px;margin:0 auto;display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap}h1{margin:0;font-size:1.6rem}.sub{opacity:.9;margin-top:5px}.status{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;background:rgba(255,255,255,.14);font-weight:700}.dot{width:10px;height:10px;border-radius:50%;background:#22c55e}.dot.offline{background:#ef4444}main{max-width:1500px;margin:20px auto;padding:0 14px 40px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}.toolbar input,.toolbar select{border:1px solid #dbe3ef;border-radius:8px;padding:9px 11px}.panel{background:#fff;border:1px solid #dbe3ef;border-radius:14px;overflow:hidden}.panel-title{padding:13px 15px;background:#eef4ff;color:#102a6b;font-weight:800}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:980px}th,td{padding:10px 9px;border-bottom:1px solid #edf1f7;text-align:center;white-space:nowrap}th{background:#f8fafc;color:#475569;font-size:.82rem}td.name{text-align:left;font-weight:700}.final td:first-child{font-weight:900;color:#102a6b}.state-final{color:#15803d;font-weight:800}.state-race{color:#2563eb;font-weight:800}.state-dnf{color:#b45309;font-weight:800}.empty{padding:40px 20px;text-align:center;color:#64748b}.official{display:none;padding:12px 15px;margin-bottom:16px;border-left:5px solid #15803d;background:#f0fdf4;color:#166534}.offline-box{display:none;padding:18px;margin-bottom:16px;background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;border-radius:10px}footer{text-align:center;color:#64748b;font-size:.8rem;margin-top:22px}@media(max-width:700px){h1{font-size:1.25rem}}</style></head>
<body><div class="top"><div class="top-inner"><div><h1>🏆 CronoAndes — Resultados</h1><div class="sub" id="event-info">Cargando evento...</div></div><div class="status"><span id="status-dot" class="dot"></span><span id="status-text">CARGANDO</span></div></div></div>
<main><div id="official-box" class="official">🏁 RESULTADOS OFICIALES PUBLICADOS</div><div id="offline-box" class="offline-box">🔴 CronoAndes no está transmitiendo resultados en este momento.</div><div class="toolbar"><input id="search" type="search" placeholder="Buscar dorsal o nombre..."><select id="category"><option value="">Todas las categorías</option></select><span id="updated">Última actualización: —</span></div><div class="panel"><div class="panel-title">Clasificación</div><div class="table-wrap"><table><thead><tr><th>Pos.</th><th>Dorsal</th><th>Nombre</th><th>Categoría</th><th>Vueltas</th><th>Estado</th><th>Tiempo Total</th><th>Dif. General</th><th>Dif. Categoría</th></tr></thead><tbody id="tbody"></tbody></table></div><div id="empty" class="empty">Esperando resultados...</div></div><footer>CronoAndes · Resultados en vivo y oficiales</footer></main>
<script src="https://cdn.socket.io/4.7.4/socket.io.min.js"></script><script>
(function(){const slug=__SLUG__;const mode=__MODE__;let payload=null;const $=id=>document.getElementById(id);function esc(v){return String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function fs(v){if(v===null||v===undefined||Number.isNaN(Number(v)))return'—';const t=Math.max(0,Number(v)),h=Math.floor(t/3600),m=Math.floor((t%3600)/60),s=Math.floor(t%60),ms=Math.floor((t-Math.floor(t))*1000);return h>0?`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`:`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`}function fd(v){if(v===null||v===undefined)return'—';return Number(v)<=1e-6?'LÍDER':`+${fs(v)}`}function sc(s){return s==='Finalizado'?'state-final':s==='En curso'?'state-race':s==='DNF'?'state-dnf':''}function cats(rows){const c=$('category'),cur=c.value;const all=[...new Set(rows.map(r=>r.categoria||'SIN CATEGORÍA'))].sort();c.innerHTML='<option value="">Todas las categorías</option>';all.forEach(x=>{const o=document.createElement('option');o.value=x;o.textContent=x;c.appendChild(o)});if(all.includes(cur))c.value=cur}function render(){if(!payload){$('empty').textContent='Esperando resultados...';return}const rows=payload.resultados||[];cats(rows);const q=$('search').value.toLowerCase().trim(),cat=$('category').value;const filtered=rows.filter(r=>(!q||String(r.dorsal||'').toLowerCase().includes(q)||String(r.nombre||'').toLowerCase().includes(q))&&(!cat||String(r.categoria||'SIN CATEGORÍA')===cat));$('tbody').innerHTML=filtered.map(r=>{const vt=Number(r.vueltas_totales||0),vh=Number(r.vueltas_completadas||0),vv=vt?`${vh}/${vt}`:`${vh}`;return `<tr class="${r.estado==='Finalizado'?'final':''}"><td><strong>${r.puesto_general??'—'}</strong></td><td><strong>${esc(r.dorsal||'')}</strong></td><td class="name">${esc(r.nombre||'')}</td><td>${esc(r.categoria||'')}</td><td>${vv}</td><td class="${sc(r.estado)}">${esc(r.estado||'')}</td><td>${fs(r.tiempo_total_seg)}</td><td>${fd(r.diferencia_general_seg)}</td><td>${fd(r.diferencia_categoria_seg)}</td></tr>`}).join('');$('empty').style.display=filtered.length?'none':'block';$('empty').textContent=rows.length?'No hay corredores que coincidan con el filtro.':'Esperando resultados...';const e=payload.evento||{};$('event-info').textContent=`${e.nombre||payload.nombre_evento||'Evento CronoAndes'}${e.etapa?' · '+e.etapa:''}${e.modalidad?' · '+String(e.modalidad).toUpperCase():''}`;$('updated').textContent=`Última actualización: ${payload.actualizado_en||'—'}`;const official=mode==='final'||payload.status==='final',live=payload.estado_evento==='en_vivo'||e.estado==='en_vivo';$('official-box').style.display=official?'block':'none';$('offline-box').style.display=official||live?'none':'block';$('status-dot').classList.toggle('offline',!official&&!live);$('status-text').textContent=official?'RESULTADOS OFICIALES':live?'EN VIVO':'SIN CONEXIÓN'}async function load(){try{const meta=await fetch(`/api/public/eventos/${encodeURIComponent(slug)}`,{cache:'no-store'});if(!meta.ok)throw Error('HTTP '+meta.status);const info=await meta.json();$('event-info').textContent=`${info.evento.nombre}${info.evento.etapa?' · '+info.evento.etapa:''}${info.evento.modalidad?' · '+String(info.evento.modalidad).toUpperCase():''}`;const ep=mode==='final'?`/api/public/final-event/${encodeURIComponent(slug)}`:`/api/public/live-event/${encodeURIComponent(slug)}`;const r=await fetch(ep,{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);payload=await r.json();render()}catch(e){console.error(e);$('status-dot').classList.add('offline');$('status-text').textContent='SIN CONEXIÓN';$('offline-box').style.display=mode==='live'?'block':'none';$('empty').textContent=mode==='live'?'Esperando conexión con CronoAndes...':'No existe un resultado oficial publicado.'}}$('search').addEventListener('input',render);$('category').addEventListener('change',render);load();const socket=io(window.location.origin,{transports:['websocket','polling'],reconnection:true});socket.on('connect',()=>{if(mode==='live'&&slug)socket.emit('subscribe',{slug})});socket.on('public_resultados',d=>{if(mode==='live'){payload=d;render()}})})();</script></body></html>""".replace('__SLUG__', safe_slug).replace('__MODE__', safe_mode)

PORTAL_HTML = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>CronoAndes — Eventos</title><style>body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:#f4f7fb;color:#0f172a}.top{background:linear-gradient(135deg,#102a6b,#173c96);color:white;padding:24px 16px}.top-inner,main{max-width:1100px;margin:0 auto}.sub{opacity:.9}main{padding:24px 14px}.panel{background:white;border:1px solid #dbe3ef;border-radius:14px;padding:18px}.event-card{display:block;color:inherit;text-decoration:none;border:1px solid #dbe3ef;border-radius:12px;padding:18px;margin-bottom:12px}.name{font-weight:800;font-size:1.12rem;color:#102a6b}.meta{margin-top:6px;color:#64748b}.badge{float:right;padding:6px 10px;border-radius:999px;background:#ecfdf5;color:#166534;font-weight:800;font-size:.78rem}.offline{background:#f8fafc}footer{text-align:center;color:#64748b;font-size:.8rem;margin-top:20px}</style></head><body><div class="top"><div class="top-inner"><h1>🏆 CronoAndes — Resultados</h1><div class="sub">Eventos en vivo y resultados oficiales</div></div></div><main><div class="panel"><div id="list">Cargando eventos...</div></div><footer>CronoAndes · Resultados en vivo y oficiales</footer></main><script>(async()=>{const list=document.getElementById('list');try{const r=await fetch('/api/public/eventos',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);const d=await r.json();const ev=d.eventos||[];if(!ev.length){list.textContent='No hay eventos disponibles en este momento.';return}list.innerHTML=ev.map(e=>`<a class="event-card ${e.estado==='en_vivo'?'':'offline'}" href="${e.live_url}"><span class="badge">${e.estado==='en_vivo'?'EN VIVO':e.estado==='finalizado'?'RESULTADOS':'OFFLINE'}</span><div class="name">${String(e.nombre||'Evento')}</div><div class="meta">${[e.etapa,e.modalidad?String(e.modalidad).toUpperCase():''].filter(Boolean).join(' · ')}</div></a>`).join('')}catch(e){console.error(e);list.textContent='No se pudo cargar el catálogo de eventos.'}})();</script></body></html>"""

@app.get("/")
def home():
    return redirect(url_for("live_page_default"))

@app.get("/live")
def live_page_default():
    return PORTAL_HTML

@app.get("/live/<slug>")
def live_page(slug):
    return build_live_html("live", slug)

@app.get("/pantalla")
def pantalla_compat():
    return PORTAL_HTML

@app.get("/resultados/<slug>")
def final_page(slug):
    return build_live_html("final", slug)


# ============================================================
# ARRANQUE
# ============================================================
if __name__ == "__main__":
    load_public_events()
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "0.0.0.0")
    logging.info("🚀 CronoAndes Public Results arrancando en %s:%s", host, port)
    logging.info("📡 URL CronoAndes: %s", resolve_server_url(force=True) or "NO DETECTADA")
    logging.info("🗃️ Snapshots: %s/%s/%s", RESULTS_REPO_OWNER, RESULTS_REPO_NAME, RESULTS_DIR)
    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=False)
