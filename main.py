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
#   - Panel de administración para gestionar eventos y resultados.

from flask import Flask, jsonify, request, redirect, url_for, Response, render_template_string, send_from_directory
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
from functools import wraps

import requests


# ============================================================
# CONFIGURACIÓN
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)


# ---------- Logo público ----------
# El archivo cronoandes-logo.png se mantiene en la raíz del repositorio.
# Esta ruta evita depender de /static/ y conserva el nombre exacto del archivo.
@app.get("/cronoandes-logo.png")
def cronoandes_logo():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(
        base_dir,
        "cronoandes-logo.png",
        mimetype="image/png",
        max_age=300,
    )

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

# Persistencia automática del último estado LIVE.
# Solo este main.py se modifica: no requiere cambios en local_server.py.
AUTO_RESULTS_DIR = os.environ.get(
    "AUTO_RESULTS_DIR", f"{RESULTS_DIR}/_auto"
).strip().strip("/")
AUTO_PERSIST_INTERVAL = max(5, int(os.environ.get("AUTO_PERSIST_INTERVAL", "10")))

PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL", "").strip()
    or os.environ.get("PUBLIC_CLOUD_URL", "").strip()
    or "https://live.say-berg.com"
).rstrip("/")

# ---------- Catálogo de eventos ----------
EVENTS_REPO_OWNER = os.environ.get(
    "EVENTS_REPO_OWNER", RESULTS_REPO_OWNER
).strip()
EVENTS_REPO_NAME = os.environ.get(
    "EVENTS_REPO_NAME", RESULTS_REPO_NAME
).strip()
EVENTS_FILE = os.environ.get(
    "EVENTS_FILE", "eventos_cronoandes.json"
).strip().strip("/")

EVENTS_GITHUB_TOKEN = (
    os.environ.get("EVENTS_GITHUB_TOKEN", "").strip()
    or RESULTS_GITHUB_TOKEN
    or GITHUB_TOKEN
)

PUBLIC_PUBLISH_TOKEN = os.environ.get(
    "PUBLIC_PUBLISH_TOKEN", ""
).strip()

# ---------- ADMIN PANEL CREDENTIALS ----------
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "cronoandes2025")

polling_interval = max(1, int(os.environ.get("POLLING_INTERVAL", "3")))


# ============================================================
# ESTADO GLOBAL
# ============================================================
SERVER_URL = ""
server_url_updated = 0.0
SERVER_URL_TTL = 15.0

pollers = {}
pollers_lock = threading.Lock()

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


def save_events_to_github(
    events,
    commit_message="Actualizar catálogo público",
    preferred_updates=None,
    replace=False,
):
    """
    Actualiza el catálogo de GitHub.

    Modos:
      - replace=False (por defecto): MERGE con lo último de GitHub
        + aplicar preferred_updates encima. Uso: upsert de un evento.
      - replace=True: SOBRESCRIBE el catálogo completo con `events`.
        Necesario para ELIMINACIONES, porque el merge reintroduce
        los eventos borrados al leer latest_events.
    """
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

                # ───── LÓGICA DIFERENCIADA ─────
                if replace:
                    # Modo eliminación: sobrescribir catálogo completo.
                    merged_events = dict(working_events)
                else:
                    # Modo upsert: merge + updates.
                    if latest_events:
                        merged_events = dict(latest_events)
                    else:
                        merged_events = dict(working_events)
                    updates = preferred_updates if preferred_updates is not None else working_events
                    merged_events.update(dict(updates or {}))
                # ───────────────────────────────

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


def delete_file_from_github(repo_owner, repo_name, file_path, token):
    """Elimina un archivo de un repositorio de GitHub."""
    api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/contents/{quote(file_path, safe='/')}"
    headers = github_api_headers(token)
    try:
        response = requests.get(api_url, headers=headers, timeout=10)
        if response.status_code == 404:
            return True, "El archivo no existe, no es necesario eliminar."
        if response.status_code != 200:
            return False, f"Error al obtener el archivo (status {response.status_code})."

        sha = response.json().get("sha")
        if not sha:
            return False, "No se pudo obtener el SHA del archivo."

        body = {
            "message": f"Eliminar archivo {file_path}",
            "sha": sha,
            "branch": "main"
        }
        delete_response = requests.delete(api_url, headers=headers, json=body, timeout=15)

        if delete_response.status_code in (200, 204):
            return True, "Archivo eliminado correctamente."
        try:
            detail = delete_response.json()
        except Exception:
            detail = delete_response.text[:500]
        return False, f"GitHub rechazó la eliminación: {detail}"

    except requests.RequestException as exc:
        return False, f"Error de conexión con GitHub: {exc}"
    except Exception as exc:
        return False, f"Error inesperado al eliminar: {exc}"


def upsert_event(event):
    global events_cache, events_cache_loaded_at
    event_code = str(event.get("event_code", "")).strip()
    if not event_code:
        return False, "event_code requerido."

    current = refresh_events_cache(force=True)
    slug = str(event.get("slug") or slugify(event.get("nombre") or event_code)).strip()

    existing = current.get(slug)
    if existing and str(existing.get("event_code", "")) != event_code:
        digest = __import__("hashlib").sha256(event_code.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}-{digest}"
        existing = current.get(slug)

    previous = existing or {}
    event = dict(event)
    event["slug"] = slug

    comparable_keys = ("event_code", "slug", "nombre", "etapa_id", "etapa", "modalidad", "estado", "server_url")
    unchanged = bool(previous) and all(previous.get(k) == event.get(k) for k in comparable_keys)
    if unchanged:
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


def delete_event_from_catalog(event_code):
    """
    Elimina un evento del catálogo de GitHub.

    Usa replace=True en save_events_to_github porque el modo merge
    reintroduce el evento borrado al leer latest_events.
    """
    if not EVENTS_GITHUB_TOKEN:
        return False, "No hay token de GitHub configurado."

    current_events = refresh_events_cache(force=True)
    slug_to_remove = None
    for slug, event in current_events.items():
        if str(event.get("event_code", "")).strip() == str(event_code).strip():
            slug_to_remove = slug
            break

    if not slug_to_remove:
        return False, f"No se encontró el evento con código {event_code} en el catálogo."

    del current_events[slug_to_remove]

    ok, detail = save_events_to_github(
        current_events,
        commit_message=f"Eliminar evento CronoAndes {event_code}",
        replace=True,
    )

    if ok:
        with events_cache_lock:
            events_cache = current_events
            events_cache_loaded_at = time.time()
        return True, f"Evento {event_code} eliminado del catálogo."
    return False, f"Error al guardar el catálogo: {detail}"


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


def get_server_urls():
    urls = []

    try:
        response = requests.get(
            GITHUB_CONFIG_URL,
            timeout=8,
            headers={"Cache-Control": "no-cache"},
        )
        response.raise_for_status()
        data = response.json()

        preferred = data.get("url_publica")
        if preferred:
            urls.append(str(preferred).strip().rstrip("/"))

        for value in (data.get("urls") or []):
            value = str(value or "").strip().rstrip("/")
            if value:
                urls.append(value)
    except Exception as exc:
        logging.warning(
            "No se pudieron leer las URLs públicas desde GitHub: %s",
            exc,
        )

    fallback = os.environ.get("SERVER_URL", "").strip().rstrip("/")
    if fallback:
        urls.append(fallback)

    unique = []
    seen = set()
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            unique.append(url)

    return unique


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

    candidatos = []
    preferred = str(server_url or "").strip().rstrip("/")
    if preferred:
        candidatos.append(preferred)

    associated = find_event_by_code(event_code)
    if associated:
        associated_url = str(
            associated.get("server_url") or ""
        ).strip().rstrip("/")
        if associated_url:
            candidatos.append(associated_url)

    candidatos.extend(get_server_urls())

    unique_candidates = []
    seen = set()
    for candidate in candidatos:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique_candidates.append(candidate)

    if not unique_candidates:
        logging.warning(
            "No hay URLs públicas disponibles para event_code=%s",
            event_code,
        )
        return None

    for candidate in unique_candidates:
        url = (
            f"{candidate}/api/public/resultados/"
            f"{quote(event_code, safe='')}"
        )
        try:
            logging.debug(
                "🌐 Consultando resultados %s mediante %s",
                event_code,
                candidate,
            )
            response = requests.get(
                url,
                timeout=8,
                headers={
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                },
            )

            if response.status_code == 200:
                payload = response.json()

                if associated is not None:
                    old_url = str(
                        associated.get("server_url") or ""
                    ).strip().rstrip("/")
                    if old_url != candidate:
                        associated["server_url"] = candidate
                        logging.info(
                            "🔄 URL LIVE actualizada en memoria: "
                            "%s | %s → %s",
                            event_code,
                            old_url or "(vacía)",
                            candidate,
                        )

                global SERVER_URL, server_url_updated
                if SERVER_URL != candidate:
                    SERVER_URL = candidate
                    server_url_updated = time.time()
                    logging.info(
                        "🔗 URL CronoAndes funcional detectada: %s",
                        candidate,
                    )
                return payload

            logging.warning(
                "⚠️ CronoAndes no respondió por %s | "
                "event_code=%s | status=%s",
                candidate,
                event_code,
                response.status_code,
            )
        except requests.RequestException as exc:
            logging.warning(
                "⚠️ Falló túnel %s para %s: %s",
                candidate,
                event_code,
                exc,
            )

    logging.warning(
        "❌ Ningún túnel disponible para event_code=%s",
        event_code,
    )
    return None


# ============================================================
# SNAPSHOT FINAL EN GITHUB
# ============================================================
def github_result_path(event_code):
    filename = f"{safe_event_code(event_code)}.json"
    return str(PurePosixPath(RESULTS_DIR) / filename)


def github_auto_result_path(event_code):
    filename = f"{safe_event_code(event_code)}.json"
    return str(PurePosixPath(AUTO_RESULTS_DIR) / filename)


def _payload_result_signature(payload):
    """Firma estable para detectar cambios reales en los resultados.

    Ignora timestamps de polling que cambian aunque no haya nuevos tiempos.
    """
    if not isinstance(payload, dict):
        return ""
    stable = dict(payload)
    stable.pop("actualizado_en", None)
    stable.pop("server_time", None)
    return json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def save_auto_snapshot(event_code, payload):
    """Guarda automáticamente el último estado LIVE en GitHub.

    No convierte el evento en oficial ni cambia su estado en el catálogo.
    Es un respaldo persistente para que el último resultado sobreviva al
    cierre de CronoAndes local o a una caída temporal del túnel.
    """
    if not RESULTS_GITHUB_TOKEN:
        return False, "RESULTS_GITHUB_TOKEN no configurado."

    event_code = str(event_code or "").strip()
    if not event_code or not isinstance(payload, dict):
        return False, "event_code o payload inválido."

    path = github_auto_result_path(event_code)
    api_url = (
        f"https://api.github.com/repos/{RESULTS_REPO_OWNER}/{RESULTS_REPO_NAME}/contents/"
        f"{quote(path, safe='/')}"
    )

    snapshot = dict(payload)
    snapshot["event_code"] = event_code
    snapshot["publicacion_activa"] = False
    snapshot["status"] = "auto_persisted"
    snapshot["tipo_publicacion"] = "automatico"
    snapshot["auto_persistido_en"] = now_iso()
    snapshot["actualizado_en"] = now_iso()

    document = json.dumps(json_safe(snapshot), ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(document.encode("utf-8")).decode("ascii")
    headers = github_api_headers()

    try:
        with github_write_lock:
            existing = requests.get(api_url, headers=headers, timeout=10)
            sha = None
            if existing.status_code == 200:
                sha = existing.json().get("sha")
            elif existing.status_code != 404:
                return False, f"GitHub GET snapshot automático devolvió {existing.status_code}."

            body = {
                "message": f"Autoguardar LIVE CronoAndes {event_code}",
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

        if response.status_code not in (200, 201):
            try:
                detail = response.json()
            except Exception:
                detail = response.text[:500]
            return False, f"GitHub rechazó el autoguardado: {detail}"

        return True, {
            "path": path,
            "raw_url": (
                f"https://raw.githubusercontent.com/{RESULTS_REPO_OWNER}/"
                f"{RESULTS_REPO_NAME}/main/{path}"
            ),
            "saved_at": snapshot["auto_persistido_en"],
        }
    except requests.RequestException as exc:
        return False, f"Error escribiendo snapshot automático: {exc}"


def load_auto_snapshot(event_code):
    path = github_auto_result_path(event_code)
    raw_url = (
        f"https://raw.githubusercontent.com/{RESULTS_REPO_OWNER}/"
        f"{RESULTS_REPO_NAME}/main/{path}"
    )
    try:
        response = requests.get(
            raw_url, timeout=8, headers={"Cache-Control": "no-cache"}
        )
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                return data
    except (requests.RequestException, ValueError) as exc:
        logging.warning("Error leyendo snapshot automático %s: %s", event_code, exc)
    return None


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
        last_persist_signature = None
        last_persist_at = 0.0

        while state["active"]:
            try:
                payload = fetch_public_results(
                    event_code,
                    server_url=None,
                )
                if payload:
                    signature = json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )

                    # Emisión en vivo: conserva el comportamiento existente.
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

                    # ========================================================
                    # AUTOPERSISTENCIA: solo main.py, sin tocar local_server.py
                    # Guarda el último estado cuando realmente cambian los
                    # resultados y limita las escrituras a GitHub.
                    # ========================================================
                    persist_signature = _payload_result_signature(payload)
                    persist_interval_ok = (
                        last_persist_at <= 0
                        or (time.time() - last_persist_at) >= AUTO_PERSIST_INTERVAL
                    )
                    estado_evento = str(payload.get("estado_evento") or "").lower()
                    debe_guardar = (
                        persist_signature
                        and persist_signature != last_persist_signature
                        and persist_interval_ok
                    )

                    # Si el emisor ya marca el evento como finalizado, guardar
                    # inmediatamente el último estado, sin esperar el intervalo.
                    if estado_evento == "finalizado" and persist_signature != last_persist_signature:
                        debe_guardar = True

                    if debe_guardar:
                        ok, detail = save_auto_snapshot(event_code, payload)
                        if ok:
                            last_persist_signature = persist_signature
                            last_persist_at = time.time()
                            logging.info(
                                "💾 Autoguardado LIVE persistente: %s | %s",
                                event_code,
                                detail.get("path") if isinstance(detail, dict) else detail,
                            )
                        else:
                            logging.warning(
                                "⚠️ No se pudo autoguardar LIVE %s: %s",
                                event_code,
                                detail,
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
# ADMIN AUTHENTICATION
# ============================================================
def check_auth(username, password):
    return username == ADMIN_USER and password == ADMIN_PASS


def authenticate():
    return Response(
        'Acceso denegado. Por favor, introduce tus credenciales.', 401,
        {'WWW-Authenticate': 'Basic realm="CronoAndes Admin"'}
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


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
        # El CronoAndes local puede haberse cerrado o el túnel puede haber
        # caído. En ese caso, mostrar el último estado persistido automáticamente.
        auto_payload = load_auto_snapshot(event_code)
        if auto_payload:
            auto_payload = dict(auto_payload)
            auto_payload.pop("event_code", None)
            auto_payload["evento"] = public_event_view(event)
            auto_payload["status"] = "auto_persisted"
            auto_payload["persistencia_automatica"] = True
            auto_payload["mensaje_persistencia"] = (
                "Mostrando el último estado guardado automáticamente. "
                "El CronoAndes local no está transmitiendo en este momento."
            )
            return jsonify(auto_payload)

        return jsonify({
            "status": "offline",
            "evento": public_event_view(event),
            "message": "CronoAndes no está transmitiendo resultados en este momento.",
        }), 503

    payload = dict(payload)
    payload.pop("event_code", None)
    payload["evento"] = public_event_view(event)
    return jsonify(payload)


@app.get("/api/public/final-event/<slug>")
def api_public_final_event(slug):
    event = find_event_by_slug(slug)
    if not event:
        return jsonify({"status": "not_found", "message": "Evento no encontrado."}), 404

    event_code = str(event.get("event_code", "")).strip()
    payload = load_final_snapshot(event_code)

    # Primero se respeta la publicación oficial existente.
    if payload and payload.get("publicacion_activa", False):
        payload = dict(payload)
        payload.pop("event_code", None)
        payload["evento"] = public_event_view(event)
        payload["status"] = "final"
        return jsonify(payload)

    # Si todavía no existe una publicación oficial, ofrecer el último
    # estado guardado automáticamente. NO se marca como resultado oficial.
    auto_payload = load_auto_snapshot(event_code)
    if auto_payload:
        auto_payload = dict(auto_payload)
        auto_payload.pop("event_code", None)
        auto_payload["evento"] = public_event_view(event)
        auto_payload["status"] = "auto_persisted"
        auto_payload["persistencia_automatica"] = True
        auto_payload["mensaje_persistencia"] = (
            "Último estado guardado automáticamente. "
            "El resultado todavía no ha sido publicado como oficial."
        )
        return jsonify(auto_payload)

    if payload and not payload.get("publicacion_activa", False):
        return jsonify({
            "status": "not_published",
            "evento": public_event_view(event),
            "message": "La publicación oficial fue retirada.",
        }), 404

    return jsonify({
        "status": "not_found",
        "evento": public_event_view(event),
        "message": "No existe un resultado guardado para este evento.",
    }), 404


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

    if payload and payload.get("publicacion_activa", False):
        return jsonify(payload)

    auto_payload = load_auto_snapshot(event_code)
    if auto_payload:
        auto_payload = dict(auto_payload)
        auto_payload["status"] = "auto_persisted"
        auto_payload["persistencia_automatica"] = True
        return jsonify(auto_payload)

    if payload and not payload.get("publicacion_activa", False):
        return jsonify({
            "status": "not_published",
            "event_code": event_code,
            "message": "La publicación oficial fue retirada.",
        }), 404

    return jsonify({
        "status": "not_found",
        "event_code": event_code,
        "message": "No existe un resultado guardado para este evento.",
    }), 404


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


def validar_publish_token(req):
    if not PUBLIC_PUBLISH_TOKEN:
        return False
    supplied = (req.headers.get("X-CronoAndes-Publish-Token", "") or "").strip()
    return supplied == PUBLIC_PUBLISH_TOKEN


@app.post("/api/public/finalizar/<event_code>")
def public_finalizar(event_code):
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

    payload["event_code"] = str(event_code).strip()
    payload["publicacion_activa"] = True
    payload["status"] = "final"
    payload["tipo_publicacion"] = "oficial"
    payload["publicado_en"] = payload.get("publicado_en") or now_iso()
    payload["actualizado_en"] = now_iso()

    ok, detail = save_final_snapshot(event_code, payload)
    if not ok:
        logging.error("❌ No se pudo guardar resultado oficial %s: %s", event_code, detail)
        return jsonify({"status": "error", "error": detail}), 502

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


@app.get("/api/public/estado/<event_code>")
def api_public_estado(event_code):
    try:
        event_code = str(event_code or "").strip()

        if not event_code:
            return jsonify({
                "ok": False,
                "error": "event_code inválido"
            }), 400

        snapshot = load_final_snapshot(event_code)

        if not snapshot:
            return jsonify({
                "ok": True,
                "event_code": event_code,
                "publicacion_activa": False,
                "final": False,
                "existe_snapshot": False
            }), 200

        publicacion_activa = bool(snapshot.get("publicacion_activa", False))
        status = str(snapshot.get("status", "")).strip().lower()

        return jsonify({
            "ok": True,
            "event_code": event_code,
            "publicacion_activa": publicacion_activa,
            "final": bool(publicacion_activa and status == "final"),
            "existe_snapshot": True,
            "status": status,
            "tipo_publicacion": snapshot.get("tipo_publicacion"),
            "etapas": snapshot.get("etapas", []),
            "actualizado_en": (
                snapshot.get("actualizado_en")
                or snapshot.get("publicado_en")
            )
        }), 200

    except Exception as exc:
        app.logger.exception("Error consultando estado de publicación")
        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


@app.route(
    "/api/public/retirar/<event_code>",
    methods=["POST"]
)
def retirar_resultados_publicos(event_code):
    try:
        if not validar_publish_token(request):
            return jsonify({
                "ok": False,
                "error": "No autorizado"
            }), 401

        event_code = str(event_code or "").strip()

        if not event_code:
            return jsonify({
                "ok": False,
                "error": "event_code inválido"
            }), 400

        snapshot = load_final_snapshot(event_code)

        if not snapshot:
            return jsonify({
                "ok": False,
                "error": "No existe una publicación para este evento"
            }), 404

        snapshot["publicacion_activa"] = False
        snapshot["status"] = "retirado"
        snapshot["actualizado_en"] = now_iso()
        snapshot["retirado_en"] = now_iso()

        ok, detail = save_final_snapshot(event_code, snapshot)
        if not ok:
            app.logger.error(
                "No se pudo retirar publicación %s: %s",
                event_code,
                detail,
            )
            return jsonify({
                "ok": False,
                "error": detail
            }), 502

        return jsonify({
            "ok": True,
            "event_code": event_code,
            "publicacion_activa": False
        })

    except Exception as e:
        app.logger.exception("Error retirando publicación")
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ============================================================
# ADMIN PANEL ENDPOINTS
# ============================================================
@app.get("/admin")
@requires_auth
def admin_dashboard():
    """Muestra el panel de administración con la lista de eventos."""
    events = refresh_events_cache(force=True)
    events_list = list(events.values())
    events_list.sort(key=lambda x: x.get("creado_en", ""), reverse=True)

    flash_ok = request.args.get("flash_ok", "")
    flash_err = request.args.get("flash_err", "")

    html = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>CronoAndes - Panel de Administración</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f4f7fb; color: #0f172a; margin: 0; padding: 20px; }
            .container { max-width: 1200px; margin: 0 auto; }
            h1 { color: #102a6b; }
            table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
            th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #dbe3ef; }
            th { background: #eef4ff; font-weight: 600; color: #102a6b; }
            tr:hover { background: #f8fafc; }
            .btn { padding: 6px 12px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; display: inline-block; font-size: 0.9em; }
            .btn-danger { background: #ef4444; color: white; }
            .btn-danger:hover { background: #dc2626; }
            .btn-secondary { background: #e2e8f0; color: #0f172a; }
            .btn-secondary:hover { background: #cbd5e1; }
            .actions { display: flex; gap: 8px; flex-wrap: wrap; }
            .badge { padding: 4px 8px; border-radius: 4px; font-size: 0.8em; font-weight: 600; }
            .badge-live { background: #dcfce7; color: #15803d; }
            .badge-final { background: #e0e7ff; color: #3730a3; }
            .empty { text-align: center; padding: 40px; color: #64748b; }
            .alert { padding: 12px 20px; border-radius: 8px; margin-bottom: 20px; }
            .alert-success { background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
            .alert-error { background: #fee2e2; color: #b91c1c; border: 1px solid #fecaca; }
            .header-actions { margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header-actions">
                <h1>🛠️ Panel de Administración</h1>
                <a href="/live" class="btn btn-secondary" target="_blank">Ver Sitio Público</a>
            </div>

            {% if flash_ok %}
                <div class="alert alert-success">{{ flash_ok }}</div>
            {% endif %}
            {% if flash_err %}
                <div class="alert alert-error">{{ flash_err }}</div>
            {% endif %}

            <table>
                <thead>
                    <tr>
                        <th>Evento</th>
                        <th>Código</th>
                        <th>Modalidad</th>
                        <th>Estado</th>
                        <th>Actualizado</th>
                        <th>Acciones</th>
                    </tr>
                </thead>
                <tbody>
                    {% for event in events %}
                    <tr>
                        <td><strong>{{ event.nombre }}</strong></td>
                        <td><code>{{ event.event_code }}</code></td>
                        <td>{{ event.modalidad or 'N/A' }}</td>
                        <td>
                            {% if event.estado == 'en_vivo' %}
                                <span class="badge badge-live">EN VIVO</span>
                            {% elif event.estado == 'finalizado' %}
                                <span class="badge badge-final">FINALIZADO</span>
                            {% else %}
                                <span class="badge">{{ event.estado }}</span>
                            {% endif %}
                        </td>
                        <td>{{ event.actualizado_en or 'N/A' }}</td>
                        <td>
                            <div class="actions">
                                <form action="/admin/delete-event/{{ event.event_code }}" method="POST" onsubmit="return confirm('¿Estás seguro de que quieres eliminar el evento {{ event.nombre }}? Esta acción no se puede deshacer.');" style="display:inline;">
                                    <button type="submit" class="btn btn-danger">Eliminar Evento</button>
                                </form>
                                <form action="/admin/delete-results/{{ event.event_code }}" method="POST" onsubmit="return confirm('¿Estás seguro de que quieres eliminar los resultados oficiales de {{ event.nombre }}?');" style="display:inline;">
                                    <button type="submit" class="btn btn-danger">Eliminar Resultados</button>
                                </form>
                            </div>
                        </td>
                    </tr>
                    {% else %}
                    <tr>
                        <td colspan="6" class="empty">No hay eventos registrados.</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """
    return render_template_string(
        html,
        events=events_list,
        flash_ok=flash_ok,
        flash_err=flash_err,
    )


@app.post("/admin/delete-event/<event_code>")
@requires_auth
def admin_delete_event(event_code):
    """Elimina un evento del catálogo."""
    ok, message = delete_event_from_catalog(event_code)
    if ok:
        return redirect(url_for('admin_dashboard', flash_ok=message))
    return redirect(url_for('admin_dashboard', flash_err=message))


@app.post("/admin/delete-results/<event_code>")
@requires_auth
def admin_delete_results(event_code):
    """Elimina el archivo de resultados oficiales de un evento."""
    if not RESULTS_GITHUB_TOKEN:
        return redirect(url_for('admin_dashboard', flash_err="No hay token de GitHub configurado."))

    path = github_result_path(event_code)
    ok, message = delete_file_from_github(
        RESULTS_REPO_OWNER,
        RESULTS_REPO_NAME,
        path,
        RESULTS_GITHUB_TOKEN
    )
    if ok:
        return redirect(url_for('admin_dashboard', flash_ok=message))
    return redirect(url_for('admin_dashboard', flash_err=message))


# ============================================================
# VISOR WEB
# ============================================================
CATALOG_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#071a2c">
<title>CronoAndes — Eventos en vivo</title>
<style>
:root{
  --bg:#f3f7fb;
  --surface:#ffffff;
  --ink:#0b1b2f;
  --muted:#607086;
  --line:#dce6f0;
  --navy:#061a2d;
  --navy-2:#0b2945;
  --lime:#9cff2f;
  --lime-2:#78ef11;
  --blue:#1388ff;
  --shadow:0 16px 40px rgba(7,26,44,.10);
  --radius:22px;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
a{color:inherit}
.site-header{position:sticky;top:0;z-index:50;background:rgba(6,26,45,.96);backdrop-filter:blur(16px);box-shadow:0 6px 24px rgba(0,0,0,.16)}
.topline{height:34px;display:flex;align-items:center;justify-content:space-between;padding:0 24px;font-size:.78rem;color:#dbe8f5;background:#041221;border-bottom:1px solid rgba(255,255,255,.06)}
.topline strong{color:#fff}
.nav{max-width:1500px;margin:0 auto;padding:13px 24px;display:flex;align-items:center;gap:24px}
.brand{display:flex;align-items:center;flex:0 0 auto;text-decoration:none}
.brand img{width:235px;height:auto;display:block;border-radius:12px;background:#011e3e}
.navlinks{display:flex;align-items:center;gap:24px;margin-left:auto}
.navlinks a{text-decoration:none;color:#dbe8f5;font-size:.92rem;font-weight:700;position:relative;transition:transform .2s ease,color .2s ease}
.navlinks a::after{content:"";position:absolute;left:0;right:0;bottom:-8px;height:2px;border-radius:99px;background:var(--lime);transform:scaleX(0);transform-origin:center;transition:transform .2s ease}
.navlinks a:hover{color:#fff;transform:translateY(-1px)}
.navlinks a:hover::after{transform:scaleX(1)}
.nav-cta{margin-left:8px;background:linear-gradient(180deg,var(--lime),#7ff018);color:#05140a!important;padding:12px 18px;border-radius:14px;box-shadow:0 10px 22px rgba(156,255,47,.22);transition:transform .2s ease,box-shadow .2s ease}
.nav-cta::after{display:none}
.nav-cta:hover{transform:translateY(-3px)!important;box-shadow:0 16px 28px rgba(156,255,47,.30)}
main{max-width:1500px;margin:0 auto;padding:34px 24px 64px}
.hero{position:relative;overflow:hidden;border-radius:30px;background:
  radial-gradient(circle at 78% 22%,rgba(156,255,47,.11),transparent 22%),
  linear-gradient(135deg,#061a2d 0%,#0a2945 58%,#0b3a5b 100%);
  color:#fff;padding:44px 44px 38px;box-shadow:var(--shadow)}
.hero::after{content:"";position:absolute;width:420px;height:420px;border-radius:50%;right:-130px;bottom:-240px;background:rgba(156,255,47,.08)}
.hero-grid{position:relative;z-index:1;display:grid;grid-template-columns:1.1fr .9fr;align-items:center;gap:36px}
.kicker{font-size:.75rem;letter-spacing:.24em;text-transform:uppercase;font-weight:900;color:var(--lime);margin-bottom:12px}
.hero h1{font-size:clamp(2rem,4vw,4rem);line-height:.98;margin:0 0 14px;max-width:760px;letter-spacing:-.045em}
.hero p{font-size:1.02rem;line-height:1.7;color:#d7e6f3;max-width:720px;margin:0}
.hero-actions{margin-top:24px;display:flex;flex-wrap:wrap;gap:12px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;text-decoration:none;border:0;cursor:pointer;font:inherit;font-weight:900;padding:12px 17px;border-radius:14px;transition:transform .18s ease,box-shadow .18s ease,filter .18s ease}
.btn:hover{transform:translateY(-3px) translateZ(0) scale(1.01);box-shadow:0 14px 24px rgba(3,16,29,.22)}
.btn:active{transform:translateY(1px) scale(.99)}
.btn-primary{background:linear-gradient(180deg,var(--lime),#7ff018);color:#061708}
.btn-ghost{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.17);color:#fff}
.hero-mini{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:24px;max-width:760px}
.mini{padding:12px 14px;border-radius:14px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.10)}
.mini strong{display:block;font-size:.82rem;margin-bottom:4px}
.mini span{display:block;color:#adc0d0;font-size:.74rem}
.hero-card{justify-self:end;width:min(100%,500px);padding:20px;border-radius:24px;background:rgba(7,26,44,.78);border:1px solid rgba(255,255,255,.12);box-shadow:0 20px 50px rgba(0,0,0,.26);backdrop-filter:blur(18px)}
.live-pill{display:inline-flex;align-items:center;gap:8px;font-weight:900;font-size:.78rem;padding:8px 12px;border-radius:999px;background:rgba(156,255,47,.10);border:1px solid rgba(156,255,47,.20);color:#eaffd7}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--lime);box-shadow:0 0 0 6px rgba(156,255,47,.08);animation:pulse 1.8s infinite}
.hero-card h2{margin:15px 0 6px;font-size:1.35rem}
.hero-card p{font-size:.92rem;color:#b8c9d8;margin:0 0 16px;line-height:1.55}
.statline{display:flex;gap:0;border-top:1px solid rgba(255,255,255,.10);padding-top:14px}
.statline div{flex:1}
.statline b{display:block;font-size:1rem;color:#fff}
.statline span{font-size:.72rem;color:#8fa8bd}
.section{padding-top:50px}
.section-head{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:18px}
.section-head h2{margin:0;font-size:clamp(1.6rem,3vw,2.25rem);letter-spacing:-.035em}
.section-head p{margin:6px 0 0;color:var(--muted)}
.events-shell{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;padding:20px}
.card{position:relative;background:#fff;border:1px solid var(--line);border-radius:20px;padding:22px;overflow:hidden;transition:transform .2s ease,box-shadow .2s ease,border-color .2s ease}
.card::before{content:"";position:absolute;inset:0 0 auto;height:4px;background:linear-gradient(90deg,var(--blue),var(--lime));opacity:.9}
.card:hover{transform:translateY(-5px);box-shadow:0 22px 38px rgba(7,26,44,.12);border-color:#c7d8e7}
.badge{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border-radius:999px;background:#edf8e5;color:#2f7a16;font-weight:900;font-size:.73rem;margin-bottom:15px}
.badge.final{background:#e8eef5;color:#476072}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.card h2{margin:0 0 8px;font-size:1.25rem;letter-spacing:-.02em}
.meta{color:var(--muted);font-size:.86rem;line-height:1.55;min-height:42px}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}
.card .btn{padding:10px 13px;border-radius:12px;font-size:.82rem}
.primary{background:var(--navy);color:#fff}
.secondary{background:#eff4f8;color:var(--ink);border:1px solid #dbe6ef}
.empty{padding:70px 20px;text-align:center;color:var(--muted)}
.footer{margin-top:40px;border-top:1px solid var(--line);padding-top:24px;display:flex;align-items:center;justify-content:space-between;gap:18px;color:var(--muted);font-size:.82rem}
.footer-brand img{width:190px;border-radius:10px;background:#011e3e}
.footer-contact{text-align:right}
.footer-contact strong{color:var(--ink)}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.55;transform:scale(.86)}}
@media (max-width:1100px){
  .navlinks{gap:16px}
  .hero-grid{grid-template-columns:1fr}
  .hero-card{justify-self:stretch;max-width:620px}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:820px){
  .topline{padding:0 14px;font-size:.70rem}
  .topline span:last-child{display:none}
  .nav{padding:10px 14px;gap:12px;flex-wrap:wrap}
  .brand img{width:200px}
  .navlinks{width:100%;order:3;justify-content:center;flex-wrap:wrap;padding-top:4px}
  .navlinks a{font-size:.83rem}
  .nav-cta{padding:10px 13px}
  main{padding:22px 14px 46px}
  .hero{padding:30px 22px;border-radius:24px}
  .hero-mini{grid-template-columns:1fr}
  .grid{grid-template-columns:1fr;padding:14px}
  .footer{flex-direction:column;align-items:flex-start}
  .footer-contact{text-align:left}
}
@media (prefers-reduced-motion:reduce){
  *{scroll-behavior:auto!important;animation:none!important;transition:none!important}
}
</style>
</head>
<body>
<header class="site-header">
  <div class="topline">
    <span><strong>Sayberg</strong> · Tecnología aplicada al deporte</span>
    <span>WhatsApp: +51 984 147 437 · sporsportandesperu@gmail.com</span>
  </div>
  <nav class="nav">
    <a class="brand" href="https://say-berg.com/" aria-label="Sayberg - Inicio">
      <img src="/cronoandes-logo.png" alt="CronoAndes — Cronometraje deportivo premium">
    </a>
    <div class="navlinks" aria-label="Navegación principal">
      <a href="https://say-berg.com/">Inicio</a>
      <a href="https://say-berg.com/servicios.html">Servicios</a>
      <a href="https://live.say-berg.com/live" aria-current="page">Resultados</a>
      <a href="https://say-berg.com/eventos.html">Eventos</a>
      <a href="https://say-berg.com/blog.html">Blog</a>
      <a href="https://say-berg.com/nosotros.html">Nosotros</a>
      <a href="https://say-berg.com/contacto.html">Contacto</a>
    </div>
    <a class="btn nav-cta" href="https://wa.me/51984147437?text=Hola%2C%20quisiera%20informaci%C3%B3n%20sobre%20el%20servicio%20de%20cronometraje%20deportivo%20de%20CronoAndes.">Solicitar cronometraje →</a>
  </nav>
</header>

<main>
  <section class="hero">
    <div class="hero-grid">
      <div>
        <div class="kicker">Cronometraje deportivo premium</div>
        <h1>Resultados que se sienten tan claros como tu competencia.</h1>
        <p>Consulta eventos, tiempos y clasificaciones de CronoAndes desde una experiencia web moderna, rápida y preparada para transmisión en vivo.</p>
        <div class="hero-actions">
          <a class="btn btn-primary" href="#events">Ver eventos en vivo ↓</a>
          <a class="btn btn-ghost" href="https://say-berg.com/contacto.html">Solicitar cronometraje →</a>
        </div>
        <div class="hero-mini">
          <div class="mini"><strong>EN VIVO</strong><span>Actualización automática</span></div>
          <div class="mini"><strong>RESULTADOS</strong><span>Clasificación por categoría</span></div>
          <div class="mini"><strong>CRONOANDES</strong><span>Powered by Sayberg</span></div>
        </div>
      </div>
      <div class="hero-card">
        <span class="live-pill"><span class="live-dot"></span> SISTEMA CRONOANDES</span>
        <h2>Resultados en vivo y oficiales</h2>
        <p>Selecciona un evento para consultar su transmisión LIVE o revisar sus resultados oficiales cuando estén publicados.</p>
        <div class="statline">
          <div><b>LIVE</b><span>Eventos activos</span></div>
          <div><b>5 s</b><span>Actualización web</span></div>
          <div><b>24/7</b><span>Consulta pública</span></div>
        </div>
      </div>
    </div>
  </section>

  <section class="section" id="events">
    <div class="section-head">
      <div>
        <div class="kicker" style="color:#0877d8;margin-bottom:8px">Eventos</div>
        <h2>Selecciona tu competencia</h2>
        <p>Consulta el LIVE o los resultados oficiales de cada evento.</p>
      </div>
    </div>
    <div class="events-shell">
      <div id="events" class="grid"><div class="empty">Cargando eventos...</div></div>
    </div>
  </section>

  <footer class="footer">
    <div class="footer-brand">
      <img src="/cronoandes-logo.png" alt="CronoAndes">
    </div>
    <div class="footer-contact">
      <strong>Sayberg · CronoAndes</strong><br>
      +51 984 147 437 · sporsportandesperu@gmail.com
    </div>
  </footer>
</main>

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

</body>
</html>"""


RESULT_PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#071a2c">
<title>CronoAndes — Resultados</title>
<style>
:root{
  --bg:#f3f7fb;
  --surface:#fff;
  --ink:#0b1b2f;
  --muted:#607086;
  --line:#dce6f0;
  --navy:#061a2d;
  --navy-2:#0a2a46;
  --lime:#9cff2f;
  --blue:#1489ff;
  --success:#2b9b3f;
  --warning:#b7791f;
  --danger:#d64545;
  --shadow:0 16px 40px rgba(7,26,44,.09);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
a{color:inherit}
.site-header{position:sticky;top:0;z-index:50;background:rgba(6,26,45,.97);backdrop-filter:blur(15px);box-shadow:0 5px 22px rgba(0,0,0,.16)}
.topline{height:34px;display:flex;align-items:center;justify-content:space-between;padding:0 24px;background:#041221;color:#d6e5f2;border-bottom:1px solid rgba(255,255,255,.06);font-size:.76rem}
.topline strong{color:#fff}
.nav{max-width:1500px;margin:0 auto;padding:11px 24px;display:flex;align-items:center;gap:22px}
.brand img{width:225px;height:auto;display:block;border-radius:11px;background:#011e3e}
.navlinks{display:flex;align-items:center;gap:22px;margin-left:auto}
.navlinks a{position:relative;color:#dbe8f5;text-decoration:none;font-weight:700;font-size:.9rem;transition:color .18s ease,transform .18s ease}
.navlinks a::after{content:"";position:absolute;left:0;right:0;bottom:-7px;height:2px;border-radius:99px;background:var(--lime);transform:scaleX(0);transition:transform .18s ease}
.navlinks a:hover{color:#fff;transform:translateY(-1px)}
.navlinks a:hover::after{transform:scaleX(1)}
.nav-cta{display:inline-flex;align-items:center;justify-content:center;margin-left:4px;padding:11px 16px;border-radius:13px;background:linear-gradient(180deg,var(--lime),#79ee13);color:#071507;text-decoration:none;font-weight:900;box-shadow:0 10px 22px rgba(156,255,47,.20);transition:transform .18s ease,box-shadow .18s ease}
.nav-cta:hover{transform:translateY(-3px) scale(1.01);box-shadow:0 15px 28px rgba(156,255,47,.28)}
.nav-cta:active{transform:translateY(1px) scale(.99)}
main{max-width:1500px;margin:0 auto;padding:26px 24px 58px}
.hero{position:relative;overflow:hidden;border-radius:28px;background:
  radial-gradient(circle at 80% 22%,rgba(156,255,47,.12),transparent 22%),
  linear-gradient(135deg,#061a2d,#0a2a46 58%,#0b3955);
  color:#fff;padding:32px 34px;box-shadow:var(--shadow)}
.hero::after{content:"";position:absolute;width:460px;height:460px;border-radius:50%;right:-150px;bottom:-310px;background:rgba(156,255,47,.06)}
.hero-row{position:relative;z-index:1;display:flex;justify-content:space-between;align-items:flex-end;gap:26px}
.kicker{font-size:.72rem;letter-spacing:.23em;text-transform:uppercase;font-weight:900;color:var(--lime);margin-bottom:9px}
.hero h1{margin:0;font-size:clamp(1.85rem,3.4vw,3.1rem);letter-spacing:-.04em;line-height:1.02}
.hero .sub{margin-top:10px;color:#c2d2df;max-width:820px;line-height:1.55}
.status{display:inline-flex;align-items:center;gap:9px;white-space:nowrap;padding:10px 13px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);border-radius:999px;font-weight:900;font-size:.76rem}
.dot{width:9px;height:9px;border-radius:50%;background:var(--lime);box-shadow:0 0 0 6px rgba(156,255,47,.08);animation:pulse 1.8s infinite}
.dot.offline{background:#ef4444;box-shadow:0 0 0 6px rgba(239,68,68,.09)}
.meta-strip{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:18px}
.event-chip{display:inline-flex;align-items:center;gap:7px;padding:8px 11px;border-radius:12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.10);font-size:.78rem;color:#d8e6f1}
main>.toolbar{margin-top:20px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.toolbar input,.toolbar select{height:44px;border:1px solid var(--line);border-radius:13px;padding:0 14px;background:#fff;color:var(--ink);box-shadow:0 5px 15px rgba(7,26,44,.04);outline:none}
.toolbar input{min-width:280px;flex:1}
.toolbar input:focus,.toolbar select:focus{border-color:#9bc9f2;box-shadow:0 0 0 4px rgba(20,137,255,.08)}
.hint{color:var(--muted);font-size:.82rem;margin-left:auto}
.notice{display:none;margin-top:16px;padding:14px 16px;border-radius:16px}
.official{border:1px solid #cde8d1;background:#f1fbf2;color:#20672b}
.offline{border:1px solid #f6d9bb;background:#fff7ed;color:#9a5b17}
.panel{margin-top:16px;background:var(--surface);border:1px solid var(--line);border-radius:22px;overflow:hidden;box-shadow:var(--shadow)}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:19px 20px;border-bottom:1px solid var(--line)}
.panel-title{font-size:1.04rem;font-weight:900;letter-spacing:-.01em}
.panel-caption{font-size:.78rem;color:var(--muted)}
.category-block{margin:0;border-bottom:1px solid var(--line)}
.category-block:last-child{border-bottom:0}
.category-title{display:flex;align-items:center;gap:10px;padding:14px 18px;background:linear-gradient(90deg,#f6fbff,#fff);color:var(--navy);font-size:.95rem;font-weight:900;border-bottom:1px solid var(--line)}
.category-title::before{content:"";width:6px;height:20px;border-radius:999px;background:linear-gradient(180deg,var(--blue),var(--lime))}
.category-table-wrap{overflow-x:auto}
.category-table{width:100%;border-collapse:collapse;min-width:980px}
.category-table th,.category-table td{padding:11px 10px;border-bottom:1px solid #edf2f6;text-align:center;white-space:nowrap}
.category-table tr:last-child td{border-bottom:0}
.category-table th{background:#fbfcfe;color:#688098;font-size:.72rem;text-transform:uppercase;letter-spacing:.07em}
.category-table tbody tr{transition:background .16s ease,transform .16s ease}
.category-table tbody tr:hover{background:#f7fbff}
.category-table td.name{text-align:left;min-width:260px;font-weight:800;color:#102438}
.category-table td:nth-child(2){font-variant-numeric:tabular-nums;font-weight:900}
.state-final{color:var(--success);font-weight:900}
.state-race{color:var(--blue);font-weight:900}
.state-dnf{color:var(--warning);font-weight:900}
.progress{font-weight:900}
.empty{padding:72px 20px;text-align:center;color:var(--muted)}
footer{margin-top:36px;padding-top:22px;border-top:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:18px;color:var(--muted);font-size:.8rem}
.footer-brand img{width:185px;border-radius:10px;background:#011e3e}
.footer-contact{text-align:right;line-height:1.55}
.footer-contact strong{color:var(--ink)}
.helper{color:#7b8b9c}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.58;transform:scale(.88)}}
@media (max-width:1100px){
  .navlinks{gap:14px}
  .hero-row{flex-direction:column;align-items:flex-start}
  .hint{margin-left:0;width:100%}
}
@media (max-width:820px){
  .topline{padding:0 14px;font-size:.68rem}
  .topline span:last-child{display:none}
  .nav{padding:9px 14px;flex-wrap:wrap;gap:10px}
  .brand img{width:195px}
  .navlinks{order:3;width:100%;justify-content:center;flex-wrap:wrap;padding-top:3px}
  .navlinks a{font-size:.81rem}
  .nav-cta{margin-left:auto;padding:9px 12px}
  main{padding:18px 12px 44px}
  .hero{padding:24px 20px;border-radius:22px}
  .hero h1{font-size:2rem}
  .toolbar input{min-width:0;width:100%}
  .toolbar select{flex:1;min-width:180px}
  .panel-head{align-items:flex-start;flex-direction:column}
  footer{align-items:flex-start;flex-direction:column}
  .footer-contact{text-align:left}
}
@media (prefers-reduced-motion:reduce){
  *,*:before,*:after{animation:none!important;transition:none!important;scroll-behavior:auto!important}
}
</style>
</head>
<body>
<header class="site-header">
  <div class="topline">
    <span><strong>Sayberg</strong> · Tecnología aplicada al deporte</span>
    <span>WhatsApp: +51 984 147 437 · sporsportandesperu@gmail.com</span>
  </div>
  <nav class="nav">
    <a class="brand" href="https://say-berg.com/" aria-label="Sayberg - Inicio">
      <img src="/cronoandes-logo.png" alt="CronoAndes — Cronometraje deportivo premium">
    </a>
    <div class="navlinks" aria-label="Navegación principal">
      <a href="https://say-berg.com/">Inicio</a>
      <a href="https://say-berg.com/servicios.html">Servicios</a>
      <a href="https://live.say-berg.com/live" aria-current="page">Resultados</a>
      <a href="https://say-berg.com/eventos.html">Eventos</a>
      <a href="https://say-berg.com/blog.html">Blog</a>
      <a href="https://say-berg.com/nosotros.html">Nosotros</a>
      <a href="https://say-berg.com/contacto.html">Contacto</a>
    </div>
    <a class="nav-cta" href="https://wa.me/51984147437?text=Hola%2C%20quisiera%20informaci%C3%B3n%20sobre%20el%20servicio%20de%20cronometraje%20deportivo%20de%20CronoAndes.">Solicitar cronometraje →</a>
  </nav>
</header>

<main>
  <section class="hero">
    <div class="hero-row">
      <div>
        <div class="kicker">Cronometraje deportivo premium</div>
        <h1 id="hero-title">Resultados en vivo</h1>
        <div id="event-info" class="sub">Cargando evento...</div>
        <div class="meta-strip">
          <span class="event-chip">Actualización automática</span>
          <span class="event-chip">Clasificación por categoría</span>
          <span class="event-chip">Powered by Sayberg</span>
        </div>
      </div>
      <div class="status"><span id="dot" class="dot"></span><span id="status">CARGANDO</span></div>
    </div>
  </section>

  <div id="official" class="notice official">✓ RESULTADOS OFICIALES PUBLICADOS</div>
  <div id="offline" class="notice offline">CronoAndes no está transmitiendo resultados en este momento. La página volverá a actualizarse cuando el sistema esté disponible.</div>

  <div class="toolbar">
    <input id="search" type="search" placeholder="Buscar dorsal o nombre..." aria-label="Buscar dorsal o nombre">
    <select id="category" aria-label="Filtrar por categoría"><option value="">Todas las categorías</option></select>
    <span id="updated" class="hint">Última actualización: —</span>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">Clasificación</div>
      <div class="panel-caption">Resultados públicos de CronoAndes</div>
    </div>
    <div id="categories"></div>
    <div id="empty" class="empty">Esperando resultados...</div>
  </div>

  <footer>
    <div class="footer-brand">
      <img src="/cronoandes-logo.png" alt="CronoAndes">
    </div>
    <div class="footer-contact">
      <strong>Sayberg · CronoAndes</strong><br>
      WhatsApp: +51 984 147 437<br>
      <span class="helper">sporsportandesperu@gmail.com</span>
    </div>
  </footer>
</main>

<script src="https://cdn.socket.io/4.7.4/socket.io.min.js"></script>
<script>
(function(){
 const path=window.location.pathname.split('/').filter(Boolean); const mode=path[0]==='resultados'?'final':'live'; const slug=decodeURIComponent(path[1]||'');
 const categories=document.getElementById('categories'),empty=document.getElementById('empty'),dot=document.getElementById('dot'),status=document.getElementById('status'),eventInfo=document.getElementById('event-info'),updated=document.getElementById('updated'),search=document.getElementById('search'),category=document.getElementById('category'),official=document.getElementById('official'),offline=document.getElementById('offline');
 let payload=null;
 function esc(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
 function fmt(v){if(v==null||Number.isNaN(Number(v)))return '—';const n=Math.max(0,Number(v)),h=Math.floor(n/3600),m=Math.floor((n%3600)/60),s=Math.floor(n%60),ms=Math.floor((n-Math.floor(n))*1000);return h?`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`:`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`}
 function diff(v){if(v==null||Number.isNaN(Number(v)))return '—';return Number(v)<=.000001?'LÍDER':'+'+fmt(v)}
 function stateClass(s){if(s==='Finalizado')return 'state-final';if(s==='En curso')return 'state-race';if(s==='DNF')return 'state-dnf';return ''}
 function render(){
     const rows=payload?.resultados||[];
     const cats=[...new Set(rows.map(r=>r.categoria||'SIN CATEGORÍA'))].sort((a,b)=>a.localeCompare(b,'es'));
     const cur=category.value;
     category.innerHTML='<option value="">Todas las categorías</option>';
     cats.forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;category.appendChild(o)});
     if(cats.includes(cur)){category.value=cur}
     const q=search.value.trim().toLowerCase();
     const selectedCat=category.value;
     const filtered=rows.filter(r=>{const dorsal=String(r.dorsal||'').toLowerCase();const nombre=String(r.nombre||'').toLowerCase();const cat=String(r.categoria||'SIN CATEGORÍA');return (!q||dorsal.includes(q)||nombre.includes(q))&&(!selectedCat||cat===selectedCat)});
     categories.innerHTML='';
     if(filtered.length){
         const grouped={};
         filtered.forEach(r=>{const cat=r.categoria||'SIN CATEGORÍA';if(!grouped[cat]){grouped[cat]=[]}grouped[cat].push(r)});
         Object.keys(grouped).sort((a,b)=>a.localeCompare(b,'es')).forEach(cat=>{
             const block=document.createElement('section');block.className='category-block';
             const title=document.createElement('div');title.className='category-title';title.textContent=`🏆 ${cat}`;
             const wrap=document.createElement('div');wrap.className='category-table-wrap';
             const table=document.createElement('table');table.className='category-table';
             table.innerHTML=`<thead><tr><th>Pos.</th><th>Dorsal</th><th>Nombre</th><th>Vueltas</th><th>Estado</th><th>Tiempo Total</th><th>Dif. General</th><th>Dif. Categoría</th></tr></thead><tbody></tbody>`;
             const tbody=table.querySelector('tbody');
             grouped[cat].forEach(r=>{const total=Number(r.vueltas_totales||0);const done=Number(r.vueltas_completadas||0);const tr=document.createElement('tr');tr.innerHTML=`<td><strong>${r.puesto_categoria??r.puesto_general??'—'}</strong></td><td><strong>${esc(r.dorsal||'')}</strong></td><td class="name">${esc(r.nombre||'')}</td><td class="progress">${total?done+'/'+total:done}</td><td class="${stateClass(r.estado)}">${esc(r.estado||'')}</td><td>${fmt(r.tiempo_total_seg)}</td><td>${diff(r.diferencia_general_seg)}</td><td>${diff(r.diferencia_categoria_seg)}</td>`;tbody.appendChild(tr)});
             wrap.appendChild(table);block.appendChild(title);block.appendChild(wrap);categories.appendChild(block);
         });
     }
     empty.style.display=filtered.length?'none':'block';
     empty.textContent=rows.length?'No hay corredores que coincidan con el filtro.':'Esperando resultados...';
     const e=payload?.evento||{};
     eventInfo.textContent=`${e.nombre||'Evento CronoAndes'}${e.etapa_id||e.etapa?' · Etapa '+(e.etapa_id||e.etapa):''}${e.modalidad?' · '+e.modalidad:''}`;
     updated.textContent='Última actualización: '+(payload?.actualizado_en||payload?.publicado_en||'—');
     const isOfficial=payload?.status==='final'||(mode==='final'&&payload?.status==='final')||e.estado==='finalizado'&&payload?.status==='final';
     const isAutoPersisted=payload?.status==='auto_persisted'||payload?.persistencia_automatica===true;
     official.style.display=isOfficial?'block':'none';
     offline.style.display=(payload?.status==='offline')?'block':'none';
     dot.classList.toggle('offline',!isOfficial&&payload?.estado_evento!=='en_vivo'&&!isAutoPersisted);
     status.textContent=isOfficial?'RESULTADOS OFICIALES':(payload?.estado_evento==='en_vivo'?'EN VIVO':(isAutoPersisted?'ÚLTIMO ESTADO GUARDADO':'SIN CONEXIÓN'));
 }
 async function load(){
   if(!slug){empty.textContent='Evento no especificado.';return}
   try{const endpoint=mode==='final'?`/api/public/final-event/${encodeURIComponent(slug)}`:`/api/public/live-event/${encodeURIComponent(slug)}`;const r=await fetch(endpoint,{cache:'no-store'});if(!r.ok)throw new Error(r.status);payload=await r.json();render()}catch(err){dot.classList.add('offline');status.textContent='SIN CONEXIÓN';offline.style.display=mode==='live'?'block':'none';empty.textContent=mode==='live'?'Esperando conexión con CronoAndes...':'No existe un resultado guardado.';empty.style.display='block'}}
 search.addEventListener('input',render);category.addEventListener('change',render);load(); if(mode==='live')setInterval(load,5000);
 const socket=io(window.location.origin,{transports:['websocket','polling'],reconnection:true,reconnectionAttempts:Infinity}); socket.on('connect',()=>{if(mode==='live')socket.emit('subscribe',{slug})});socket.on('public_resultados',d=>{if(mode==='live'){payload=d;payload.evento=payload.evento||{};render()}});
})();
</script>
</body>
</html>"""


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
