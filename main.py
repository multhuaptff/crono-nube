# main.py
# Visor de resultados CronoAndes - Modo Proxy con detección automática de URL desde GitHub

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, join_room, emit
import os
import requests
import logging
import json
import time
import threading
from datetime import datetime

# === Configuración ===
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'cronoandes-secure-key-2025')
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

# URL del archivo de configuración en GitHub
GITHUB_CONFIG_URL = 'https://raw.githubusercontent.com/multhuaptff/crono-server-ciclismo/main/crono_server_url.json'

def get_server_url():
    """Obtiene la URL del servidor local desde GitHub."""
    try:
        resp = requests.get(GITHUB_CONFIG_URL, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # Priorizar URL pública (túnel)
            if data.get('url_publica'):
                return data['url_publica'].rstrip('/')
            elif data.get('urls') and len(data['urls']) > 0:
                return data['urls'][0].rstrip('/')
        logging.warning("No se pudo obtener URL desde GitHub, usando variable SERVER_URL")
    except Exception as e:
        logging.error(f"Error obteniendo URL desde GitHub: {e}")
    
    # Fallback a variable de entorno
    return os.environ.get('SERVER_URL', '').strip()

SERVER_URL = get_server_url()
if not SERVER_URL:
    logging.warning("SERVER_URL no configurada. Las peticiones fallarán.")
else:
    logging.info(f"Visor conectado al servidor: {SERVER_URL}")

# === Funciones auxiliares ===
def fetch_inscritos(event_code):
    """Obtiene la lista de inscritos desde el servidor local."""
    if not SERVER_URL:
        return []
    try:
        url = f"{SERVER_URL}/api/inscritos/{event_code}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        else:
            logging.error(f"Error obteniendo inscritos: {resp.status_code}")
            return []
    except Exception as e:
        logging.error(f"Error en fetch_inscritos: {e}")
        return []

def fetch_tiempos(event_code):
    """Obtiene los tiempos desde el servidor local."""
    if not SERVER_URL:
        return []
    try:
        url = f"{SERVER_URL}/api/tiempos/{event_code}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        else:
            logging.error(f"Error obteniendo tiempos: {resp.status_code}")
            return []
    except Exception as e:
        logging.error(f"Error en fetch_tiempos: {e}")
        return []

# === WebSockets y polling ===
polling_active = False
polling_interval = 3  # segundos
polling_event_code = None
last_tiempos_data = {}

def start_polling(event_code):
    """Inicia un hilo que consulta periódicamente los tiempos."""
    global polling_active, polling_event_code, last_tiempos_data
    if polling_active and polling_event_code == event_code:
        return
    polling_active = True
    polling_event_code = event_code
    
    def poll():
        global polling_active, last_tiempos_data
        while polling_active:
            try:
                tiempos = fetch_tiempos(event_code)
                if tiempos:
                    # Enviar todos los tiempos a los clientes para reconstruir la tabla
                    socketio.emit('nuevo_tiempo', tiempos, room=event_code)
                time.sleep(polling_interval)
            except Exception as e:
                logging.error(f"Error en polling: {e}")
                time.sleep(polling_interval)
    
    thread = threading.Thread(target=poll, daemon=True)
    thread.start()

@socketio.on('subscribe')
def on_subscribe(data):
    event_code = data.get('event_code', '').strip()
    if event_code:
        join_room(event_code)
        logging.info(f"Cliente suscrito a evento: {event_code}")
        start_polling(event_code)

# === API del visor ===
@app.route('/api/inscritos/<event_code>')
def proxy_inscritos(event_code):
    """Proxy para obtener inscritos desde el servidor local."""
    return jsonify(fetch_inscritos(event_code))

@app.route('/api/tiempos/<event_code>')
def proxy_tiempos(event_code):
    """Proxy para obtener tiempos desde el servidor local."""
    return jsonify(fetch_tiempos(event_code))

@app.route('/api/refresh/<event_code>')
def refresh(event_code):
    """Fuerza una actualización de los tiempos desde el servidor local."""
    tiempos = fetch_tiempos(event_code)
    socketio.emit('nuevo_tiempo', tiempos, room=event_code)
    return jsonify({"status": "ok", "count": len(tiempos)})

# === Página principal ===
@app.route('/')
def home():
    return '''
    <h2>⏱️ CronoAndes - Visor de Resultados</h2>
    <p>Conectado al servidor: <strong>''' + SERVER_URL + '''</strong></p>
    <p>Accede a la <a href="/pantalla?event_code=TU_CODIGO">pantalla en vivo</a> para ver resultados.</p>
    '''

# === Pantalla en vivo ===
@app.route('/pantalla')
def pantalla_vivo():
    return '''
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🏆 CronoAndes — Resultados en Vivo</title>
    <style>
        body {
            font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
            background: #0f172a;
            color: white;
            margin: 0;
            padding: 0;
            overflow-x: hidden;
        }
        .header {
            text-align: center;
            padding: 1rem;
            background: #1e293b;
            border-bottom: 3px solid #38bdf8;
        }
        .logo {
            max-height: 70px;
            margin-bottom: 12px;
            border-radius: 6px;
        }
        .header h1 {
            font-size: 2.0rem;
            margin: 0.5rem 0;
            color: white;
            text-shadow: 0 0 8px rgba(56, 189, 248, 0.6);
        }
        .contador-maestro {
            font-size: 1.3rem;
            font-weight: bold;
            color: #60a5fa;
            margin-top: 0.5rem;
            font-family: 'Courier New', monospace;
        }
        .categoria-seccion {
            margin: 2rem 1rem;
            border: 1px solid #334155;
            border-radius: 10px;
            background: #1e293b;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        .categoria-titulo {
            background: #0f172a;
            color: #f8fafc;
            padding: 14px;
            font-size: 1.5rem;
            font-weight: bold;
            text-align: center;
            border-bottom: 2px solid #38bdf8;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
            margin-top: 8px;
        }
        th, td {
            padding: 12px 10px;
            text-align: center;
            font-size: 1.2rem;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        th {
            background: #0f172a;
            color: #94a3b8;
            font-weight: bold;
            font-size: 1.1rem;
        }
        .finalizado {
            background-color: #1e293b !important;
            color: #60a5fa !important;
        }
        .pos { width: 8%; }
        .dorsal { width: 15%; }
        .nombre { width: 35%; }
        .categoria-col { width: 22%; }
        .tiempo { width: 20%; }
    </style>
</head>
<body>
    <div class="header">
        <img id="logo" class="logo" style="display:none;">
        <h1>🏆 CronoAndes — Resultados en Vivo</h1>
        <div class="contador-maestro">⏰ Cargando resultados en vivo...</div>
    </div>
    <div id="contenedor-categorias"></div>

    <script>
    document.addEventListener('DOMContentLoaded', function() {
        const urlParams = new URLSearchParams(window.location.search);
        let eventCode = urlParams.get('event_code');
        const logoUrl = urlParams.get('logo_url');

        if (logoUrl) {
            const img = document.getElementById('logo');
            img.src = logoUrl;
            img.style.display = 'block';
        }

        if (!eventCode) {
            eventCode = prompt("Ingresa el código del evento:");
            if (!eventCode) {
                document.body.innerHTML = '<div style="color:white;text-align:center;padding:4rem;font-size:1.5rem;background:#0f172a;">❌ Código del evento requerido</div>';
                return;
            }
            let newUrl = `${window.location.pathname}?event_code=${encodeURIComponent(eventCode)}`;
            if (logoUrl) newUrl += `&logo_url=${encodeURIComponent(logoUrl)}`;
            window.history.replaceState(null, null, newUrl);
        }

        const socket = io(window.location.origin, { transports: ['websocket'] });
        socket.emit('subscribe', { event_code: eventCode });

        let registros = {};
        let inscritos = {};
        let inicioOficial = null;
        let intervalId = null;

        function formatearCronometroMaestro(ms) {
            if (ms == null || ms < 0) return '00:00.000';
            const totalSegundos = ms / 1000;
            const mins = Math.floor(totalSegundos / 60);
            const segs = Math.floor(totalSegundos % 60);
            const milis = Math.floor(ms % 1000);
            return `${mins.toString().padStart(2, '0')}:${segs.toString().padStart(2, '0')}.${milis.toString().padStart(3, '0')}`;
        }

        function formatearTiempoCompetidor(ms) {
            if (ms == null) return '';
            const totalSegundos = ms / 1000;
            const mins = Math.floor(totalSegundos / 60);
            const segs = Math.floor(totalSegundos % 60);
            const milis = Math.floor(ms % 1000);
            return `${mins.toString().padStart(2, '0')}:${segs.toString().padStart(2, '0')}.${milis.toString().padStart(3, '0')}`;
        }

        function calcularTiempo(dorsal) {
            const r = registros[dorsal] || { salidas: [], llegadas: [] };
            if (!r.salidas.length || !r.llegadas.length) return null;
            const lastSalida = r.salidas[r.salidas.length - 1];
            const lastLlegada = r.llegadas[r.llegadas.length - 1];
            const s = new Date(lastSalida.endsWith('Z') ? lastSalida : lastSalida + 'Z');
            const l = new Date(lastLlegada.endsWith('Z') ? lastLlegada : lastLlegada + 'Z');
            if (isNaN(s) || isNaN(l) || l < s) return null;
            return l - s;
        }

        function procesar(t) {
            if (!registros[t.dorsal]) registros[t.dorsal] = { salidas: [], llegadas: [] };
            const tsNorm = t.timestamp.endsWith('Z') ? t.timestamp : t.timestamp + 'Z';
            const eventoTime = new Date(tsNorm);

            if (t.action === 'salida') {
                registros[t.dorsal].salidas.push(t.timestamp);
                if (!inicioOficial) {
                    inicioOficial = eventoTime;
                    if (!intervalId) {
                        intervalId = setInterval(() => {
                            if (inicioOficial) {
                                const ahora = new Date();
                                const transcurrido = ahora - inicioOficial;
                                document.querySelector('.contador-maestro').textContent = 
                                    `⏱️ En vivo: ${formatearCronometroMaestro(transcurrido)}`;
                            }
                        }, 20);
                    }
                }
            } else if (t.action === 'llegada') {
                registros[t.dorsal].llegadas.push(t.timestamp);
            }

            if (t.nombre && !inscritos[t.dorsal]) {
                inscritos[t.dorsal] = {
                    dorsal: t.dorsal,
                    nombre: t.nombre,
                    categoria: t.categoria || 'SIN CATEGORÍA'
                };
            }
        }

        function renderizar() {
            const competidores = Object.keys(inscritos).map(d => {
                const tiempo = calcularTiempo(d);
                return tiempo !== null ? {
                    dorsal: d,
                    nombre: inscritos[d].nombre,
                    categoria: inscritos[d].categoria,
                    tiempo: tiempo
                } : null;
            }).filter(Boolean);

            const porCategoria = {};
            competidores.forEach(c => {
                if (!porCategoria[c.categoria]) porCategoria[c.categoria] = [];
                porCategoria[c.categoria].push(c);
            });

            Object.keys(porCategoria).forEach(cat => {
                porCategoria[cat].sort((a, b) => a.tiempo - b.tiempo);
                porCategoria[cat].forEach((c, i) => c.pos = i + 1);
            });

            const categoriasOrdenadas = Object.keys(porCategoria).sort();

            let html = '';
            if (categoriasOrdenadas.length === 0) {
                html = '<div style="text-align:center;padding:2.5rem;color:#94a3b8;font-size:1.2rem;">Esperando primeros tiempos...</div>';
            } else {
                categoriasOrdenadas.forEach(cat => {
                    const filas = porCategoria[cat].map(f => `
                        <tr class="finalizado">
                            <td class="pos">${f.pos}</td>
                            <td class="dorsal">${f.dorsal}</td>
                            <td class="nombre">${f.nombre}</td>
                            <td class="categoria-col">${f.categoria}</td>
                            <td class="tiempo">${formatearTiempoCompetidor(f.tiempo)}</td>
                        </tr>
                    `).join('');
                    html += `
                        <div class="categoria-seccion">
                            <div class="categoria-titulo">${cat}</div>
                            <table>
                                <thead>
                                    <tr>
                                        <th class="pos">Pos</th>
                                        <th class="dorsal">Dorsal</th>
                                        <th class="nombre">Nombre</th>
                                        <th class="categoria-col">Categoría</th>
                                        <th class="tiempo">Tiempo</th>
                                    </tr>
                                </thead>
                                <tbody>${filas}</tbody>
                            </table>
                        </div>
                    `;
                });
            }

            document.getElementById('contenedor-categorias').innerHTML = html;
        }

        // Carga inicial
        Promise.all([
            fetch(`/api/inscritos/${encodeURIComponent(eventCode)}`).then(r => r.ok ? r.json() : []),
            fetch(`/api/tiempos/${encodeURIComponent(eventCode)}`).then(r => r.ok ? r.json() : [])
        ]).then(([inscritosData, tiemposData]) => {
            inscritos = {};
            inscritosData.forEach(p => {
                inscritos[p.dorsal] = {
                    dorsal: p.dorsal,
                    nombre: p.nombre,
                    categoria: p.categoria || 'SIN CATEGORÍA'
                };
            });
            tiemposData.forEach(t => procesar(t));
            renderizar();
        }).catch(err => {
            console.error("Error al cargar datos iniciales:", err);
        });

        // Escuchar eventos de WebSocket
        socket.on('nuevo_tiempo', (data) => {
            // Si es un array (actualización por polling), procesar cada uno
            if (Array.isArray(data)) {
                // Limpiar y reconstruir desde cero
                registros = {};
                data.forEach(t => procesar(t));
            } else {
                procesar(data);
            }
            renderizar();
        });
    });
    </script>
    <script src="https://cdn.socket.io/4.7.4/socket.io.min.js"></script>
</body>
</html>
    '''

# === Health check ===
@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "app": "CronoAndes Proxy",
        "server_url": SERVER_URL,
        "connected": bool(SERVER_URL)
    })

# === Iniciar ===
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
