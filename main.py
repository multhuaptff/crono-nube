# main.py
# CronoAndes - Portal público multi-evento LIVE + RESULTADOS OFICIALES
#
# Objetivos:
#   - Mantener compatibilidad con la API anterior basada en event_code.
#   - Añadir catálogo público por slug/nombre.
#   - Permitir múltiples CronoAndes/eventos simultáneos, cada uno con su server_url.
#   - No mostrar event_code al público.
#   - Persistir el catálogo de eventos en GitHub para sobrevivir reinicios de Render.
#   - Publicar snapshots finales en un repositorio de resultados separado.
#
# Este archivo es un reemplazo directo de main.py en crono-nube.

from flask import Flask, jsonify, request, redirect, url_for
from flask_cors import CORS
from flask_socketio import SocketIO, join_room
import base64
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import quote

import requests


# ============================================================
# CONFIGURACIÓN
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "cronoandes-secure-key-2025"
)

allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*").strip()
cors_origins = "*" if allowed_origins == "*" else [
    x.strip() for x in allowed_origins.split(",") if x.strip()
]
CORS(app, resources={r"/*": {"origins": cors_origins}})

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="gevent",
    ping_interval=25,
    ping_timeout=60,
)

# ---------- Descubrimiento legacy ----------
GITHUB_CONFIG_URL = os.environ.get(
    "GITHUB_CONFIG_URL",
    "https://raw.githubusercontent.com/multhuaptff/crono-server-ciclismo/main/crono_server_url.json",
).strip()

# ---------- GitHub ----------
# Compatibilidad con configuración actual de CronoAndes
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

RESULTS_GITHUB_TOKEN = (
    os.environ.get("RESULTS_GITHUB_TOKEN", "").strip()
    or GITHUB_TOKEN
)

RESULTS_REPO_OWNER = os.environ.get(
    "RESULTS_REPO_OWNER",
    os.environ.get("REPO_OWNER", "multhuaptff")
).strip()

RESULTS_REPO_NAME = os.environ.get(
    "RESULTS_REPO_NAME",
    os.environ.get("REPO_NAME", "crono-server-ciclismo")
).strip()

RESULTS_DIR = os.environ.get(
    "RESULTS_DIR", "resultados_cronoandes"
).strip().strip("/")

PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL", "").strip()
    or os.environ.get("PUBLIC_CLOUD_URL", "").strip()
    or "https://live.say-berg.com"
).rstrip("/")

# ---------- Catálogo de eventos ----------
# Puede estar en el mismo repo de resultados para no provocar redeploy del servicio.
EVENTS_REPO_OWNER = os.environ.get(
    "EVENTS_REPO_OWNER", RESULTS_REPO_OWNER
).strip()
EVENTS_REPO_NAME = os.environ.get(
    "EVENTS_REPO_NAME", RESULTS_REPO_NAME
).strip()
EVENTS_FILE = os.environ.get(
    "EVENTS_FILE", "eventos_cronoandes.json"
).strip().strip("/")

# Token específico del catálogo.
# Si no existe, utiliza RESULTS_GITHUB_TOKEN o GITHUB_TOKEN.
EVENTS_GITHUB_TOKEN = (
    os.environ.get("EVENTS_GITHUB_TOKEN", "").strip()
    or RESULTS_GITHUB_TOKEN
    or GITHUB_TOKEN
)

# Token compartido únicamente entre CronoAndes y crono-nube.
PUBLIC_PUBLISH_TOKEN = os.environ.get(
    "PUBLIC_PUBLISH_TOKEN", ""
).strip()

polling_interval = max(1, int(os.environ.get("POLLING_INTERVAL", "3")))


# ============================================================
# ESTADO GLOBAL
# ============================================================
SERVER_URL = ""
server_url_updated = 0.0
SERVER_URL_TTL = 15.0

# event_code -> {active, thread}
pollers = {}
pollers_lock = threading.Lock()

# slug -> metadata del evento.
events_cache = {}
events_cache_loaded_at = 0.0
events_cache_ttl = 10.0
events_cache_lock = threading.RLock()

github_write_lock = threading.Lock()


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


def slugify(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"^-+|-+$", "", value)
    return value[:90] or "evento"


def public_event_view(event):
    """Devuelve solamente información segura para el navegador público."""
    if not isinstance(event, dict):
        return {}
    return {
        "slug": event.get("slug", ""),
        "nombre": event.get("nombre", "Evento CronoAndes"),
        "etapa_id": event.get("etapa_id"),
        "etapa": event.get("etapa") or event.get("etapa_id"),
        "modalidad": event.get("modalidad", ""),
        "estado": event.get("estado", "en_vivo"),
        "creado_en": event.get("creado_en"),
        "actualizado_en": event.get("actualizado_en"),
        "live_url": f"/live/{quote(event.get('slug', ''), safe='')}",
        "resultados_url": f"/resultados/{quote(event.get('slug', ''), safe='')}",
        "url_live": f"{PUBLIC_BASE_URL}/live/{quote(event.get('slug', ''), safe='')}",
        "url_resultados": f"{PUBLIC_BASE_URL}/resultados/{quote(event.get('slug', ''), safe='')}",
    }


def github_api_headers(token=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = token if token is not None else RESULTS_GITHUB_TOKEN
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def event_file_api_url():
    return (
        f"https://api.github.com/repos/{EVENTS_REPO_OWNER}/"
        f"{EVENTS_REPO_NAME}/contents/{quote(EVENTS_FILE, safe='/')}"
    )


def load_events_from_github():
    """Lee el catálogo persistido en GitHub. Fallos => catálogo vacío."""
    if not EVENTS_GITHUB_TOKEN:
        # En modo sin token no intentamos usar la API autenticada;
        # aun así permitimos una lectura RAW pública.
        raw_url = (
            f"https://raw.githubusercontent.com/{EVENTS_REPO_OWNER}/"
            f"{EVENTS_REPO_NAME}/main/{EVENTS_FILE}"
        )
        try:
            response = requests.get(raw_url, timeout=8, headers={"Cache-Control": "no-cache"})
            if response.status_code == 200:
                data = response.json()
                return data if isinstance(data, dict) else {}
        except requests.RequestException:
            pass
        return {}

    try:
        response = requests.get(
            event_file_api_url(),
            timeout=10,
            headers={**github_api_headers(EVENTS_GITHUB_TOKEN), "Cache-Control": "no-cache"},
        )
        if response.status_code == 200:
            info = response.json()
            encoded = info.get("content", "").replace("\n", "")
            if not encoded:
                return {}
            document = base64.b64decode(encoded).decode("utf-8")
            data = json.loads(document)
            return data if isinstance(data, dict) else {}
        if response.status_code == 404:
            return {}
        logging.warning("GitHub catálogo GET status=%s", response.status_code)
    except (requests.RequestException, ValueError, UnicodeDecodeError) as exc:
        logging.warning("No se pudo leer catálogo de eventos: %s", exc)
    return {}


def refresh_events_cache(force=False):
    global events_cache_loaded_at, events_cache
    with events_cache_lock:
        if (
            not force
            and (time.time() - events_cache_loaded_at) < events_cache_ttl
        ):
            return dict(events_cache)
        raw = load_events_from_github()
        events = raw.get("eventos", {}) if isinstance(raw, dict) else {}
        if isinstance(events, list):
            converted = {}
            for item in events:
                if isinstance(item, dict) and item.get("slug"):
                    converted[item["slug"]] = item
            events = converted
        if not isinstance(events, dict):
            events = {}
        events_cache = dict(events)
        events_cache_loaded_at = time.time()
        return dict(events_cache)


def save_events_to_github(events, commit_message="Actualizar catálogo público", preferred_updates=None):
    """Actualiza el catálogo con merge seguro contra la última versión de GitHub."""
    if not EVENTS_GITHUB_TOKEN:
        return False, "EVENTS_GITHUB_TOKEN/RESULTS_GITHUB_TOKEN no configurado; no se puede persistir catálogo."

    with github_write_lock:
        working_events = dict(events or {})
        for attempt in range(3):
            headers = github_api_headers(EVENTS_GITHUB_TOKEN)
            api_url = event_file_api_url()
            sha = None
            try:
                existing = requests.get(api_url, headers=headers, timeout=10)
                latest_events = {}
                if existing.status_code == 200:
                    existing_info = existing.json()
                    sha = existing_info.get("sha")
                    encoded = existing_info.get("content", "").replace("\n", "")
                    if encoded:
                        try:
                            latest_document = base64.b64decode(encoded).decode("utf-8")
                            latest_data = json.loads(latest_document)
                            latest_events = latest_data.get("eventos", {}) if isinstance(latest_data, dict) else {}
                            if not isinstance(latest_events, dict):
                                latest_events = {}
                        except (ValueError, UnicodeDecodeError):
                            latest_events = {}
                elif existing.status_code != 404:
                    return False, f"GitHub GET catálogo devolvió {existing.status_code}."

                # Fusiona con lo último que existe en GitHub.
                # En actualizaciones multi-evento NO volvemos a escribir todo el
                # snapshot local, porque podría estar desactualizado y sobrescribir
                # cambios recientes de otro CronoAndes. Solo aplicamos las claves
                # que esta operación modificó explícitamente.
                if latest_events:
                    merged_events = dict(latest_events)
                else:
                    merged_events = dict(working_events)
                updates = preferred_updates if preferred_updates is not None else working_events
                merged_events.update(dict(updates or {}))

                document = json.dumps(
                    {"version": 1, "actualizado_en": now_iso(), "eventos": merged_events},
                    ensure_ascii=False,
                    indent=2,
                )
                content_b64 = base64.b64encode(document.encode("utf-8")).decode("ascii")
                body = {
                    "message": commit_message,
                    "content": content_b64,
                    "branch": "main",
                }
                if sha:
                    body["sha"] = sha

                response = requests.put(
                    api_url,
                    headers=headers,
                    json=body,
                    timeout=15,
                )
                if response.status_code in (200, 201):
                    return True, response.json()
                # 409 => otro equipo escribió primero; repetir leyendo y fusionando
                # el catálogo actualizado.
                if response.status_code == 409 and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text[:500]
                return False, f"GitHub rechazó catálogo: {detail}"
            except requests.RequestException as exc:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return False, f"Error escribiendo catálogo en GitHub: {exc}"
    return False, "No fue posible guardar catálogo."


def upsert_event(event):
    global events_cache, events_cache_loaded_at
    event_code = str(event.get("event_code", "")).strip()
    if not event_code:
        return False, "event_code requerido."

    # Recarga para integrar cambios de otros PCs antes de fusionar.
    current = refresh_events_cache(force=True)
    slug = str(event.get("slug") or slugify(event.get("nombre") or event_code)).strip()

    # Evita colisión de slug entre dos códigos distintos.
    existing = current.get(slug)
    if existing and str(existing.get("event_code", "")) != event_code:
        # Diferenciador estable que no revela el event_code al público.
        digest = __import__("hashlib").sha256(event_code.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}-{digest}"
        existing = current.get(slug)

    previous = existing or {}
    # Normalizar el evento con el slug definitivo antes de compararlo.
    # Evita commits repetidos cuando hubo una colisión de slug.
    event = dict(event)
    event["slug"] = slug

    comparable_keys = ("event_code", "slug", "nombre", "etapa_id", "etapa", "modalidad", "estado", "server_url")
    unchanged = bool(previous) and all(previous.get(k) == event.get(k) for k in comparable_keys)
    if unchanged:
        # El evento no ha cambiado: no hacemos un nuevo commit a GitHub.
        # Esto evita escrituras periódicas innecesarias mientras CronoAndes
        # envía heartbeats/actualizaciones repetidas.
        return True, previous

    merged = dict(previous)
    merged.update(event)
    merged["slug"] = slug
    merged["event_code"] = event_code
    merged["creado_en"] = previous.get("creado_en") or event.get("creado_en") or now_iso()
    merged["actualizado_en"] = now_iso()
    merged["nombre"] = str(merged.get("nombre") or "Evento CronoAndes").strip()
    merged["estado"] = str(merged.get("estado") or "en_vivo").strip()

    current[slug] = merged
    ok, detail = save_events_to_github(
        current,
        commit_message=f"Registrar/actualizar evento CronoAndes {event_code}",
        preferred_updates={slug: merged},
    )
    if ok:
        with events_cache_lock:
            events_cache = current
            events_cache_loaded_at = time.time()
        return True, merged
    return False, detail


def find_event_by_slug(slug):
    slug = str(slug or "").strip()
    events = refresh_events_cache()
    return events.get(slug)


def find_event_by_code(event_code):
    code = str(event_code or "").strip()
    events = refresh_events_cache()
    for event in events.values():
        if str(event.get("event_code", "")).strip() == code:
            return event
    return None


# ============================================================
# DESCUBRIMIENTO LEGACY DEL TÚNEL
# ============================================================
def get_server_url():
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
        logging.warning("No se pudo resolver URL desde GitHub: %s", exc)
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
        logging.info("🔗 URL CronoAndes actualizada (legacy): %s", resolved)
    SERVER_URL = resolved
    server_url_updated = time.time()
    return SERVER_URL


# ============================================================
# API HACIA CRONOANDES
# ============================================================
def fetch_public_results(event_code, server_url=None):
    event_code = str(event_code or "").strip()
    if not event_code:
        return None

    # En multi-evento, preferimos el server_url asociado al evento.
    server_url = (server_url or "").strip().rstrip("/")
    if not server_url:
        associated = find_event_by_code(event_code)
        if associated:
            server_url = str(associated.get("server_url") or "").strip().rstrip("/")
    if not server_url:
        server_url = resolve_server_url()
    if not server_url:
        return None

    url = f"{server_url}/api/public/resultados/{quote(event_code, safe='')}"
    try:
        response = requests.get(
            url,
            timeout=8,
            headers={"Accept": "application/json"},
        )
        if response.status_code == 200:
            return response.json()

        # Si falla y el evento no tiene URL específica, intentar legacy.
        if response.status_code in (404, 409, 502, 503, 504):
            associated_event = find_event_by_code(event_code)
            if not associated_event:
                refreshed = resolve_server_url(force=True)
                if refreshed and refreshed != server_url:
                    retry_url = f"{refreshed}/api/public/resultados/{quote(event_code, safe='')}"
                    retry = requests.get(
                        retry_url,
                        timeout=8,
                        headers={"Accept": "application/json"},
                    )
                    if retry.status_code == 200:
                        return retry.json()
        logging.warning("GET resultados event_code=%s status=%s", event_code, response.status_code)
    except requests.RequestException as exc:
        logging.warning("Error consultando CronoAndes %s: %s", event_code, exc)
    return None


# ============================================================
# SNAPSHOT FINAL EN GITHUB
# ============================================================
def github_result_path(event_code):
    filename = f"{safe_event_code(event_code)}.json"
    return str(PurePosixPath(RESULTS_DIR) / filename)


def save_final_snapshot(event_code, payload):
    if not RESULTS_GITHUB_TOKEN:
        return False, "RESULTS_GITHUB_TOKEN no configurado."

    path = github_result_path(event_code)
    api_url = (
        f"https://api.github.com/repos/{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}/contents/"
        f"{quote(path, safe='/')}"
    )
    document = json.dumps(json_safe(payload), ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(document.encode("utf-8")).decode("ascii")
    headers = github_api_headers()
    sha = None
    try:
        existing = requests.get(api_url, headers=headers, timeout=10)
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
        response = requests.put(api_url, headers=headers, json=body, timeout=15)
    except requests.RequestException as exc:
        return False, f"Error escribiendo en GitHub: {exc}"
    if response.status_code not in (200, 201):
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:500]
        return False, f"GitHub rechazó la publicación: {detail}"
    return True, {
        "path": path,
        "raw_url": (
            f"https://raw.githubusercontent.com/{RESULTS_REPO_OWNER}/"
            f"{RESULTS_REPO_NAME}/main/{path}"
        ),
        "published_at": now_iso(),
    }


def load_final_snapshot(event_code):
    path = github_result_path(event_code)
    raw_url = (
        f"https://raw.githubusercontent.com/{RESULTS_REPO_OWNER}/"
        f"{RESULTS_REPO_NAME}/main/{path}"
    )
    try:
        response = requests.get(
            raw_url, timeout=8, headers={"Cache-Control": "no-cache"}
        )
        if response.status_code == 200:
            return response.json()
    except requests.RequestException as exc:
        logging.warning("Error leyendo snapshot final %s: %s", event_code, exc)
    return None


# ============================================================
# POLLING + SOCKET.IO
# ============================================================
def start_polling(event_code, server_url=None):
    """
    Inicia un único poller por event_code.

    Si el poller ya existe y está activo, NO crea otro hilo:
    únicamente actualiza server_url para que una renovación de túnel
    tenga efecto inmediato en el siguiente ciclo de polling.
    """
    event_code = str(event_code or "").strip()
    if not event_code:
        return

    normalized_url = str(server_url or "").strip().rstrip("/")

    with pollers_lock:
        existing = pollers.get(event_code)

        if existing and existing.get("active"):
            previous_url = str(existing.get("server_url") or "").strip().rstrip("/")

            if normalized_url and normalized_url != previous_url:
                existing["server_url"] = normalized_url
                logging.info(
                    "🔄 Poller existente actualizado para %s: %s -> %s",
                    event_code,
                    previous_url or "(sin URL)",
                    normalized_url,
                )
            return

        state = {
            "active": True,
            "server_url": normalized_url,
        }
        pollers[event_code] = state

    logging.info(
        "▶️ Polling iniciado para %s cada %ss | URL=%s",
        event_code,
        polling_interval,
        normalized_url or "(auto)",
    )

    def poll():
        last_signature = None

        while state["active"]:
            try:
                payload = fetch_public_results(
                    event_code,
                    server_url=state.get("server_url") or None,
                )
                if payload:
                    signature = json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if signature != last_signature:
                        last_signature = signature
                        public_payload = dict(payload)
                        if find_event_by_code(event_code):
                            public_payload.pop("event_code", None)
                        socketio.emit("public_resultados", public_payload, room=event_code)
                        socketio.emit(
                            "nuevo_tiempo",
                            public_payload.get("resultados", []),
                            room=event_code,
                        )
                socketio.sleep(polling_interval)

            except Exception as exc:
                logging.error("Error polling %s: %s", event_code, exc, exc_info=True)
                socketio.sleep(polling_interval)

        logging.info("⏹️ Polling detenido para %s", event_code)

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
    slug = str(data.get("slug", "")).strip()
    event_code = str(data.get("event_code", "")).strip()

    if slug:
        event = find_event_by_slug(slug)
        if event:
            event_code = str(event.get("event_code", "")).strip()
    if not event_code:
        return

    join_room(event_code)
    logging.info("👀 Cliente suscrito a evento público: %s", slug or "legacy")
    event = find_event_by_code(event_code)
    start_polling(event_code, server_url=(event or {}).get("server_url"))


# ============================================================
# API PÚBLICA
# ============================================================
@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "app": "CronoAndes Public Results",
            "server_url": resolve_server_url(),
            "polling_interval": polling_interval,
            "result_repository": f"{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}",
            "events_file": f"{EVENTS_REPO_OWNER}/{EVENTS_REPO_NAME}/{EVENTS_FILE}",
            "events_storage_ready": bool(EVENTS_GITHUB_TOKEN),
            "server_time": now_iso(),
        }
    )


@app.get("/api/status")
def status():
    with pollers_lock:
        active_events = sorted(
            code for code, state in pollers.items() if state.get("active")
        )
    return jsonify(
        {
            "status": "ok",
            "server_url": resolve_server_url(),
            "polling_interval": polling_interval,
            "polling_events": active_events,
            "eventos_registrados": len(refresh_events_cache()),
            "results_repo": f"{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}",
        }
    )


@app.post("/api/public/registrar-evento")
def registrar_evento():
    """Registra/actualiza un evento de CronoAndes en el catálogo público."""
    if not PUBLIC_PUBLISH_TOKEN:
        return jsonify({"status": "error", "error": "PUBLIC_PUBLISH_TOKEN no configurado."}), 503

    supplied = request.headers.get("X-CronoAndes-Publish-Token", "").strip()
    if supplied != PUBLIC_PUBLISH_TOKEN:
        return jsonify({"status": "error", "error": "Token de publicación inválido."}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "JSON inválido."}), 400

    event_code = str(payload.get("event_code", "")).strip()
    nombre = str(
        payload.get("nombre")
        or payload.get("evento")
        or payload.get("nombre_evento")
        or payload.get("event_name")
        or payload.get("event_title")
        or "Evento CronoAndes"
    ).strip()
    if not event_code:
        return jsonify({"status": "error", "error": "event_code requerido."}), 400

    event = {
        "event_code": event_code,
        "nombre": nombre,
        "slug": str(payload.get("slug") or slugify(nombre)).strip(),
        "etapa_id": payload.get("etapa_id"),
        "etapa": payload.get("etapa"),
        "modalidad": str(payload.get("modalidad", "")).strip(),
        "estado": str(payload.get("estado") or payload.get("status") or "en_vivo").strip(),
        "server_url": str(payload.get("server_url") or "").strip().rstrip("/"),
    }

    ok, detail = upsert_event(event)
    if not ok:
        logging.error("❌ No se pudo registrar evento público %s: %s", event_code, detail)
        return jsonify({"status": "error", "error": detail}), 502

    logging.info(
        "🌐 Evento público registrado: %s | slug=%s | estado=%s",
        detail.get("nombre"),
        detail.get("slug"),
        detail.get("estado"),
    )
    if detail.get("server_url"):
        logging.info("🔗 LIVE público: /live/%s", detail.get("slug"))
        logging.info("🏁 RESULTADOS públicos: /resultados/%s", detail.get("slug"))

    start_polling(event_code, server_url=detail.get("server_url"))

    return jsonify({
        "status": "ok",
        "evento": public_event_view(detail),
    })


@app.get("/api/public/eventos")
def api_public_eventos():
    events = refresh_events_cache(force=True)
    public_events = [public_event_view(event) for event in events.values()]
    public_events.sort(
        key=lambda e: (e.get("estado") != "en_vivo", e.get("nombre", "").lower())
    )
    return jsonify({
        "status": "ok",
        "server_time": now_iso(),
        "eventos": public_events,
    })


@app.get("/api/public/eventos/<slug>")
def api_public_evento(slug):
    event = find_event_by_slug(slug)
    if not event:
        return jsonify({"status": "not_found", "message": "Evento no encontrado."}), 404
    return jsonify({"status": "ok", "evento": public_event_view(event)})


@app.get("/api/public/live-event/<slug>")
def api_public_live_event(slug):
    event = find_event_by_slug(slug)
    if not event:
        return jsonify({"status": "not_found", "message": "Evento no encontrado."}), 404
    event_code = str(event.get("event_code", "")).strip()
    payload = fetch_public_results(event_code, server_url=event.get("server_url"))
    if not payload:
        return jsonify({
            "status": "offline",
            "evento": public_event_view(event),
            "message": "CronoAndes no está transmitiendo resultados en este momento.",
        }), 503
    # Ocultamos event_code del payload entregado al navegador.
    payload = dict(payload)
    payload.pop("event_code", None)
    payload["evento"] = public_event_view(event)
    return jsonify(payload)


@app.get("/api/public/final-event/<slug>")
def api_public_final_event(slug):
    event = find_event_by_slug(slug)
    if not event:
        return jsonify({"status": "not_found", "message": "Evento no encontrado."}), 404
    payload = load_final_snapshot(str(event.get("event_code", "")).strip())
    if not payload:
        return jsonify({
            "status": "not_found",
            "evento": public_event_view(event),
            "message": "No existe un resultado oficial publicado.",
        }), 404
    payload = dict(payload)
    payload.pop("event_code", None)
    payload["evento"] = public_event_view(event)
    payload["status"] = "final"
    return jsonify(payload)


# ---------- Compatibilidad legacy ----------
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
    event = find_event_by_code(event_code)
    payload = fetch_public_results(event_code, server_url=(event or {}).get("server_url"))
    if payload:
        socketio.emit("public_resultados", payload, room=event_code)
        socketio.emit("nuevo_tiempo", payload.get("resultados", []), room=event_code)
    return jsonify({
        "status": "ok" if payload else "offline",
        "event_code": event_code,
        "count": len(payload.get("resultados", [])) if payload else 0,
    })


@app.post("/api/public/finalizar/<event_code>")
def public_finalizar(event_code):
    """Recibe snapshot desde CronoAndes y lo deja como oficial."""
    if not PUBLIC_PUBLISH_TOKEN:
        return jsonify({
            "status": "error",
            "error": "PUBLIC_PUBLISH_TOKEN no configurado en crono-nube.",
        }), 503

    supplied = request.headers.get("X-CronoAndes-Publish-Token", "").strip()
    if supplied != PUBLIC_PUBLISH_TOKEN:
        return jsonify({"status": "error", "error": "Token de publicación inválido."}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "error": "JSON de publicación inválido."}), 400

    if str(payload.get("event_code", "")).strip() != str(event_code).strip():
        return jsonify({"status": "error", "error": "event_code inconsistente."}), 400

    payload["status"] = "final"
    payload["tipo_publicacion"] = "oficial"
    payload["publicado_en"] = payload.get("publicado_en") or now_iso()

    # Persistir resultado oficial.
    ok, detail = save_final_snapshot(event_code, payload)
    if not ok:
        logging.error("❌ No se pudo guardar resultado oficial %s: %s", event_code, detail)
        return jsonify({"status": "error", "error": detail}), 502

    # Marcar catálogo como finalizado.
    event = find_event_by_code(event_code)
    if event:
        updated = dict(event)
        updated["estado"] = "finalizado"
        updated["actualizado_en"] = now_iso()
        updated_ok, updated_detail = upsert_event(updated)
        if not updated_ok:
            logging.warning(
                "⚠️ Resultado oficial %s guardado, pero no se pudo actualizar el catálogo: %s",
                event_code,
                updated_detail,
            )
        event = updated if updated_ok else event

    slug = (event or {}).get("slug") or slugify((event or {}).get("nombre") or event_code)
    logging.info("🏁 Resultado oficial guardado: %s", event_code)
    return jsonify({
        "status": "ok",
        "event_code": event_code,
        "resultado": detail,
        "live_url": f"/live/{quote(slug, safe='')}",
        "final_url": f"/resultados/{quote(slug, safe='')}",
    })


# ============================================================
# VISOR WEB
# ============================================================
CATALOG_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CronoAndes — Eventos</title>
<style>
:root{--bg:#f4f7fb;--panel:#fff;--text:#0f172a;--muted:#64748b;--line:#dbe3ef;--header:#102a6b;--header2:#173c96;--accent:#2563eb;--success:#15803d}
*{box-sizing:border-box}body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text)}
.top{background:linear-gradient(135deg,var(--header),var(--header2));color:#fff;padding:24px 16px;box-shadow:0 3px 12px rgba(15,23,42,.2)}
.wrap{max-width:1200px;margin:auto}.top h1{margin:0;font-size:1.8rem}.top p{margin:6px 0 0;opacity:.88}
main{max-width:1200px;margin:22px auto;padding:0 14px 50px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 5px 18px rgba(15,23,42,.05)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;padding:16px}.card{border:1px solid var(--line);border-radius:12px;padding:18px;background:#fff}.card h2{margin:0 0 7px;font-size:1.12rem}.meta{color:var(--muted);font-size:.92rem;margin-bottom:14px}.badge{display:inline-flex;gap:6px;align-items:center;font-weight:800;font-size:.82rem;padding:6px 9px;border-radius:999px;background:#eef4ff;color:var(--accent);margin-bottom:12px}.badge.final{background:#f0fdf4;color:var(--success)}
.btns{display:flex;gap:8px;flex-wrap:wrap}.btn{display:inline-block;padding:9px 12px;border-radius:8px;text-decoration:none;font-weight:800;font-size:.88rem}.primary{background:var(--accent);color:#fff}.secondary{background:#eef2f7;color:var(--text)}.copy{cursor:pointer;border:0}
.empty{padding:44px 20px;text-align:center;color:var(--muted)}footer{text-align:center;color:var(--muted);font-size:.82rem;margin-top:20px}
</style>
</head>
<body>
<div class="top"><div class="wrap"><h1>🏆 CronoAndes — Resultados</h1><p>Eventos en vivo y resultados oficiales</p></div></div>
<main><div class="panel"><div id="events" class="grid"><div class="empty">Cargando eventos...</div></div></div><footer>CronoAndes · Resultados en vivo y oficiales</footer></main>
<script>
(function(){
 const box=document.getElementById('events');
 function escapeHtml(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
 function card(e){
   const final=e.estado==='finalizado';
   const liveUrl=e.live_url||('/live/'+encodeURIComponent(e.slug));
   const finalUrl=e.resultados_url||('/resultados/'+encodeURIComponent(e.slug));
   return `<article class="card">
      <div class="badge ${final?'final':''}">${final?'🏁 FINALIZADO':'🟢 EN VIVO'}</div>
      <h2>${escapeHtml(e.nombre)}</h2>
      <div class="meta">${e.etapa_id||e.etapa?`Etapa ${escapeHtml(e.etapa_id||e.etapa)} · `:''}${escapeHtml(e.modalidad||'')}</div>
      <div class="btns">
        <a class="btn primary" href="${liveUrl}">Ver LIVE</a>
        <a class="btn secondary" href="${finalUrl}">${final?'Ver resultados':'Resultados'}</a>
        <button class="btn secondary copy" onclick="copyLink('${location.origin}${liveUrl}',this)">Copiar LIVE</button>
      </div>
   </article>`;
 }
 window.copyLink=async function(link,btn){try{await navigator.clipboard.writeText(link);const old=btn.textContent;btn.textContent='✓ Copiado';setTimeout(()=>btn.textContent=old,1500)}catch(e){window.prompt('Copia este enlace:',link)}};
 async function load(){
   try{
     const r=await fetch('/api/public/eventos',{cache:'no-store'}); if(!r.ok) throw new Error(r.status);
     const data=await r.json(); const events=data.eventos||[];
     box.innerHTML=events.length?events.map(card).join(''):'<div class="empty">No hay eventos disponibles en este momento.</div>';
   }catch(e){box.innerHTML='<div class="empty">No se pudo cargar el catálogo. Reintentando...</div>';setTimeout(load,5000)}
 }
 load();setInterval(load,10000);
})();
</script>
</body></html>"""


RESULT_PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CronoAndes — Resultados</title>
<style>
:root{--bg:#f4f7fb;--panel:#fff;--text:#0f172a;--muted:#64748b;--line:#dbe3ef;--header:#102a6b;--header2:#173c96;--accent:#2563eb;--success:#15803d;--warning:#b45309}
*{box-sizing:border-box}body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text)}
.top{background:linear-gradient(135deg,var(--header),var(--header2));color:#fff;padding:20px 16px;position:sticky;top:0;z-index:10;box-shadow:0 3px 12px rgba(15,23,42,.18)}
.top-inner{max-width:1500px;margin:auto;display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap}.top h1{margin:0;font-size:1.6rem}.sub{opacity:.9;margin-top:5px}.status{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;background:rgba(255,255,255,.14);font-weight:800}.dot{width:10px;height:10px;border-radius:50%;background:#22c55e}.dot.offline{background:#ef4444}
main{max-width:1500px;margin:20px auto;padding:0 14px 40px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}.toolbar input,.toolbar select{border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:#fff}.hint{color:var(--muted);font-size:.88rem}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 5px 18px rgba(15,23,42,.05)}
.panel-title{padding:13px 15px;background:#eef4ff;color:var(--header);font-size:1.05rem;font-weight:800;border-bottom:1px solid var(--line)}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:980px}th,td{padding:10px 9px;border-bottom:1px solid #edf1f7;text-align:center;white-space:nowrap}th{background:#f8fafc;color:#475569;font-size:.82rem;text-transform:uppercase;letter-spacing:.03em}td.name{text-align:left;min-width:240px;font-weight:700}.state-final{color:var(--success);font-weight:800}.state-race{color:var(--accent);font-weight:800}.state-dnf{color:var(--warning);font-weight:800}.progress{font-weight:800}.empty{padding:40px 20px;text-align:center;color:var(--muted)}.official{display:none;border-left:5px solid var(--success);padding:12px 15px;background:#f0fdf4;color:#166534;margin-bottom:16px;border-radius:8px}.offline{display:none;padding:18px;border-radius:10px;background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;margin-bottom:18px}footer{text-align:center;color:var(--muted);font-size:.8rem;margin-top:22px}
</style>
</head>
<body>
<div class="top"><div class="top-inner"><div><h1>🏆 CronoAndes — Resultados</h1><div id="event-info" class="sub">Cargando evento...</div></div><div class="status"><span id="dot" class="dot"></span><span id="status">CARGANDO</span></div></div></div>
<main><div id="official" class="official">🏁 RESULTADOS OFICIALES PUBLICADOS</div><div id="offline" class="offline">🔴 CronoAndes no está transmitiendo resultados en este momento. La página volverá a actualizarse cuando el sistema esté disponible.</div>
<div class="toolbar"><input id="search" type="search" placeholder="Buscar dorsal o nombre..."><select id="category"><option value="">Todas las categorías</option></select><span id="updated" class="hint">Última actualización: —</span></div>
<div class="panel"><div class="panel-title">Clasificación</div><div class="table-wrap"><table><thead><tr><th>Pos.</th><th>Dorsal</th><th>Nombre</th><th>Categoría</th><th>Vueltas</th><th>Estado</th><th>Tiempo Total</th><th>Dif. General</th><th>Dif. Categoría</th></tr></thead><tbody id="tbody"></tbody></table></div><div id="empty" class="empty">Esperando resultados...</div></div><footer>CronoAndes · Resultados en vivo y oficiales</footer></main>
<script src="https://cdn.socket.io/4.7.4/socket.io.min.js"></script>
<script>
(function(){
 const path=window.location.pathname.split('/').filter(Boolean); const mode=path[0]==='resultados'?'final':'live'; const slug=decodeURIComponent(path[1]||'');
 const tbody=document.getElementById('tbody'),empty=document.getElementById('empty'),dot=document.getElementById('dot'),status=document.getElementById('status'),eventInfo=document.getElementById('event-info'),updated=document.getElementById('updated'),search=document.getElementById('search'),category=document.getElementById('category'),official=document.getElementById('official'),offline=document.getElementById('offline');
 let payload=null;
 function esc(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
 function fmt(v){if(v==null||Number.isNaN(Number(v)))return '—';const n=Math.max(0,Number(v)),h=Math.floor(n/3600),m=Math.floor((n%3600)/60),s=Math.floor(n%60),ms=Math.floor((n-Math.floor(n))*1000);return h?`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`:`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`}
 function diff(v){if(v==null||Number.isNaN(Number(v)))return '—';return Number(v)<=.000001?'LÍDER':'+'+fmt(v)}
 function stateClass(s){if(s==='Finalizado')return 'state-final';if(s==='En curso')return 'state-race';if(s==='DNF')return 'state-dnf';return ''}
 function render(){
   const rows=payload?.resultados||[]; const cats=[...new Set(rows.map(r=>r.categoria||'SIN CATEGORÍA'))].sort(); const cur=category.value; category.innerHTML='<option value="">Todas las categorías</option>'; cats.forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;category.appendChild(o)});if(cats.includes(cur))category.value=cur;
   const q=search.value.trim().toLowerCase(),cat=category.value; const filtered=rows.filter(r=>(!q||String(r.dorsal||'').toLowerCase().includes(q)||String(r.nombre||'').toLowerCase().includes(q))&&(!cat||String(r.categoria||'')===cat));
   tbody.innerHTML=filtered.map(r=>{const total=Number(r.vueltas_totales||0),done=Number(r.vueltas_completadas||0);return `<tr><td><strong>${r.puesto_general??'—'}</strong></td><td><strong>${esc(r.dorsal||'')}</strong></td><td class="name">${esc(r.nombre||'')}</td><td>${esc(r.categoria||'')}</td><td class="progress">${total?done+'/'+total:done}</td><td class="${stateClass(r.estado)}">${esc(r.estado||'')}</td><td>${fmt(r.tiempo_total_seg)}</td><td>${diff(r.diferencia_general_seg)}</td><td>${diff(r.diferencia_categoria_seg)}</td></tr>`}).join('');
   empty.style.display=filtered.length?'none':'block'; empty.textContent=rows.length?'No hay corredores que coincidan con el filtro.':'Esperando resultados...';
   const e=payload?.evento||{}; eventInfo.textContent=`${e.nombre||'Evento CronoAndes'}${e.etapa_id||e.etapa?' · Etapa '+(e.etapa_id||e.etapa):''}${e.modalidad?' · '+e.modalidad:''}`; updated.textContent='Última actualización: '+(payload?.actualizado_en||payload?.publicado_en||'—');
   const isFinal=mode==='final'||payload?.status==='final'||e.estado==='finalizado'; official.style.display=isFinal?'block':'none'; offline.style.display=(payload?.status==='offline')?'block':'none'; dot.classList.toggle('offline',!isFinal&&payload?.estado_evento!=='en_vivo'); status.textContent=isFinal?'RESULTADOS OFICIALES':(payload?.estado_evento==='en_vivo'?'EN VIVO':'SIN CONEXIÓN');
 }
 async function load(){
   if(!slug){empty.textContent='Evento no especificado.';return}
   try{const endpoint=mode==='final'?`/api/public/final-event/${encodeURIComponent(slug)}`:`/api/public/live-event/${encodeURIComponent(slug)}`;const r=await fetch(endpoint,{cache:'no-store'});if(!r.ok)throw new Error(r.status);payload=await r.json();render()}catch(err){dot.classList.add('offline');status.textContent='SIN CONEXIÓN';offline.style.display=mode==='live'?'block':'none';empty.textContent=mode==='live'?'Esperando conexión con CronoAndes...':'No existe un resultado oficial publicado.';empty.style.display='block'}}
 search.addEventListener('input',render);category.addEventListener('change',render);load(); if(mode==='live')setInterval(load,5000);
 const socket=io(window.location.origin,{transports:['websocket','polling'],reconnection:true,reconnectionAttempts:Infinity}); socket.on('connect',()=>{if(mode==='live')socket.emit('subscribe',{slug})});socket.on('public_resultados',d=>{if(mode==='live'){payload=d;payload.evento=payload.evento||{};render()}});
})();
</script>
</body></html>"""


@app.get("/")
def home():
    return redirect(url_for("live_catalog"))


@app.get("/live")
def live_catalog():
    return CATALOG_HTML


@app.get("/live/<slug>")
def live_page(slug):
    return RESULT_PAGE


@app.get("/resultados/<slug>")
def final_page(slug):
    return RESULT_PAGE


@app.get("/pantalla")
def pantalla_compat():
    return RESULT_PAGE


# ============================================================
# ARRANQUE
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "0.0.0.0")
    logging.info("🚀 CronoAndes Public Results arrancando en %s:%s", host, port)
    logging.info("📡 URL CronoAndes legacy: %s", resolve_server_url(force=True) or "NO DETECTADA")
    logging.info(
        "🗃️ Snapshots: %s/%s/%s",
        RESULTS_REPO_OWNER,
        RESULTS_REPO_NAME,
        RESULTS_DIR,
    )
    logging.info(
        "📚 Catálogo: %s/%s/%s",
        EVENTS_REPO_OWNER,
        EVENTS_REPO_NAME,
        EVENTS_FILE,
    )
    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=False)
