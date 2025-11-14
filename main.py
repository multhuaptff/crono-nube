# main.py
# Aplicación oficial: CronoAndes
# Sistema de cronometraje deportivo en tiempo real – Formato Copa del Mundo

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, join_room
import os
import psycopg2
from datetime import datetime, timezone
import logging

# === Configuración ===
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'cronoandes-secure-key-2025')
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

# === Base de datos ===
def get_db_conn():
    db_url = os.environ.get('DATABASE_URL', '').strip()
    if not db_url:
        raise Exception("DATABASE_URL no está definida")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url, sslmode='require')

def init_db():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tiempos (
            id SERIAL PRIMARY KEY,
            evento TEXT NOT NULL,
            dorsal TEXT NOT NULL,
            action TEXT NOT NULL,
            timestamp_iso TEXT NOT NULL,
            creado_en TIMESTAMP DEFAULT NOW()
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS inscritos (
            id SERIAL PRIMARY KEY,
            event_code TEXT NOT NULL,
            dorsal TEXT NOT NULL,
            nombre TEXT NOT NULL,
            categoria TEXT NOT NULL,
            club TEXT NOT NULL,
            rfid TEXT,
            creado_en TIMESTAMP DEFAULT NOW()
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_inscritos_event ON inscritos (event_code)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tiempos_evento ON tiempos (evento)')
    conn.commit()
    cur.close()
    conn.close()

def is_db_ready():
    """Verifica si la base de datos está lista para uso (útil en Render)"""
    db_url = os.environ.get('DATABASE_URL', '').strip()
    return bool(db_url) and not db_url.startswith('https://api.render.com')

# === WebSockets ===
@socketio.on('connect')
def handle_connect():
    logging.info(f"Nuevo cliente conectado: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    logging.info(f"Cliente desconectado: {request.sid}")

@socketio.on('subscribe')
def on_subscribe(data):
    event_code = data.get('event_code', '').strip()
    if event_code:
        join_room(event_code)
        logging.info(f"Cliente {request.sid} suscrito a evento: {event_code}")

# === API: Recibir tiempos ===
@app.route('/api/crono', methods=['POST'])
def crono():
    try:
        if not is_db_ready():
            return jsonify({"error": "base de datos no lista"}), 503
        init_db()
        data = request.get_json()
        if not data:
            return jsonify({"error": "JSON inválido"}), 400

        dorsal = str(data.get('dorsal', '')).strip()
        action = str(data.get('action', 'llegada')).strip().lower()
        provided_ts = data.get('timestamp')
        if provided_ts:
            ts = str(provided_ts).strip()
            if not ts.endswith('Z') and '+' not in ts and 'Z' not in ts:
                ts = ts.rstrip() + 'Z'
        else:
            ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        event_code = str(data.get('event_code', 'demo')).strip()

        if not dorsal or not event_code:
            return jsonify({"error": "dorsal y event_code requeridos"}), 400

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tiempos (evento, dorsal, action, timestamp_iso) VALUES (%s, %s, %s, %s)",
            (event_code, dorsal, action, ts)
        )
        conn.commit()

        cur.execute('''
            SELECT nombre, categoria FROM inscritos 
            WHERE event_code = %s AND dorsal = %s
        ''', (event_code, dorsal))
        row = cur.fetchone()
        nombre = row[0] if row else ""
        categoria = row[1] if row else ""

        cur.close()
        conn.close()

        socketio.emit('nuevo_tiempo', {
            'event_code': event_code,
            'dorsal': dorsal,
            'action': action,
            'timestamp': ts,
            'nombre': nombre,
            'categoria': categoria
        }, room=event_code)

        return jsonify({"status": "success"}), 201
    except Exception as e:
        logging.error(f"Error en /api/crono: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/tiempos/<event_code>')
def tiempos(event_code):
    try:
        if not is_db_ready():
            return jsonify([])
        init_db()
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT dorsal, action, timestamp_iso FROM tiempos WHERE evento = %s ORDER BY id", (event_code,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([{"dorsal": r[0], "action": r[1], "timestamp": r[2]} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === API: Inscripciones — SEPARADO EN DOS ENDPOINTS ===

@app.route('/api/inscritos/<event_code>', methods=['POST'])
def recibir_inscritos(event_code):
    """Recibe lista de inscritos desde Streamlit"""
    try:
        if not is_db_ready():
            return jsonify({"error": "base de datos no lista"}), 503
        init_db()
        data = request.get_json()
        if not isinstance(data, list):
            return jsonify({"error": "esperaba una lista"}), 400

        event_code = event_code.strip()
        conn = get_db_conn()
        cur = conn.cursor()
        # 1. Eliminar inscritos anteriores
        cur.execute("DELETE FROM inscritos WHERE event_code = %s", (event_code,))
        # 2. Insertar los nuevos
        count = 0
        for item in data:
            dorsal = str(item.get('dorsal', '')).strip()
            nombre = str(item.get('nombre', '')).strip()
            categoria = str(item.get('categoria', '')).strip()
            club = str(item.get('club', '')).strip()
            rfid = str(item.get('rfid', '')).strip()
            if dorsal and nombre and categoria and club:
                cur.execute('''
                    INSERT INTO inscritos (event_code, dorsal, nombre, categoria, club, rfid)
                    VALUES (%s, %s, %s, %s, %s, %s)
                ''', (event_code, dorsal, nombre, categoria, club, rfid))
                count += 1
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success", "count": count}), 201
    except Exception as e:
        logging.error(f"Error en POST /api/inscritos/{event_code}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/inscritos/<event_code>', methods=['GET'])
def obtener_inscritos(event_code):
    """Devuelve lista de inscritos a la app móvil o pantalla en vivo"""
    try:
        if not is_db_ready():
            return jsonify([])
        init_db()
        event_code = event_code.strip()
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute('''
            SELECT dorsal, nombre, categoria, club, rfid 
            FROM inscritos 
            WHERE event_code = %s 
            ORDER BY dorsal
        ''', (event_code,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([{
            "dorsal": r[0], "nombre": r[1], "categoria": r[2], "club": r[3], "rfid": r[4]
        } for r in rows])
    except Exception as e:
        logging.error(f"Error en GET /api/inscritos/{event_code}: {e}")
        return jsonify({"error": str(e)}), 500

# === API: Borrar datos ===
@app.route('/api/flush-event/<event_code>', methods=['DELETE'])
def flush_event(event_code):
    try:
        if not is_db_ready():
            return jsonify({"error": "base de datos no lista"}), 503
        init_db()
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM tiempos WHERE evento = %s", (event_code.strip(),))
        count = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success", "deleted": count}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/flush-inscritos/<event_code>', methods=['DELETE'])
def flush_inscritos(event_code):
    try:
        if not is_db_ready():
            return jsonify({"error": "base de datos no lista"}), 503
        init_db()
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM inscritos WHERE event_code = %s", (event_code.strip(),))
        count = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success", "deleted": count}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === Página principal ===
@app.route('/')
def home():
    return '''
    <h2>⏱️ CronoAndes - Sistema de Cronometraje Profesional</h2>
    <p>Este sistema está en modo <strong>TIEMPO REAL</strong>.</p>
    <p>Accede a la <a href="/pantalla?event_code=TU_CODIGO">pantalla en vivo</a> para ver resultados.</p>
    <p>✅ Formato Copa del Mundo<br>✅ Logo personalizable<br>✅ Agrupado por categoría</p>
    '''

# === Pantalla en vivo — CRONOANDES (Formato Copa del Mundo) ===
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
        <div class="contador-maestro">⏰ Esperando primera salida...</div>
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
            const s = new Date((r.salidas[0].endsWith('Z') ? r.salidas[0] : r.salidas[0] + 'Z'));
            const l = new Date((r.llegadas[0].endsWith('Z') ? r.llegadas[0] : r.llegadas[0] + 'Z'));
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
                porCategoria[cat].forEach((c, i) => c.pos = i + 1;
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

        socket.on('nuevo_tiempo', (d) => {
            procesar(d);
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
    try:
        if not is_db_ready():
            return jsonify({"status": "db_not_ready"}), 503
        init_db()
        return jsonify({"status": "ok", "app": "CronoAndes", "websocket_ready": True})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# === Iniciar ===
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
