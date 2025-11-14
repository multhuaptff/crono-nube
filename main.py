# main.py
# ¡Este archivo reemplaza completamente a Streamlit!
# Despliéguelo en Render como aplicación web estándar (no Streamlit)

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, join_room
import os
import psycopg2
from datetime import datetime
import logging
import json

# === Configuración ===
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'downhill-secure-key-2025')
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

# === API: Recibir y servir tiempos ===
@app.route('/api/crono', methods=['POST'])
def crono():
    try:
        init_db()
        data = request.get_json()
        if not data:
            return jsonify({"error": "JSON inválido"}), 400

        dorsal = str(data.get('dorsal', '')).strip()
        action = str(data.get('action', 'llegada')).strip().lower()
        ts = str(data.get('timestamp', datetime.utcnow().isoformat())).strip()
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

        # Obtener datos del participante
        cur.execute('''
            SELECT nombre, categoria FROM inscritos 
            WHERE event_code = %s AND dorsal = %s
        ''', (event_code, dorsal))
        row = cur.fetchone()
        nombre = row[0] if row else ""
        categoria = row[1] if row else ""

        cur.close()
        conn.close()

        # Emitir en tiempo real
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

# === API: Inscripciones ===
@app.route('/api/inscritos/<event_code>', methods=['POST', 'GET'])
def manejar_inscritos(event_code):
    try:
        init_db()
        if request.method == 'POST':
            data = request.get_json()
            if not isinstance(data, list):
                return jsonify({"error": "esperaba una lista"}), 400

            conn = get_db_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM inscritos WHERE event_code = %s", (event_code.strip(),))
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

        else:  # GET
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
        return jsonify({"error": str(e)}), 500

# === API: Borrar datos ===
@app.route('/api/flush-event/<event_code>', methods=['DELETE'])
def flush_event(event_code):
    try:
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

# === Página principal: redirige a pantalla en vivo ===
@app.route('/')
def home():
    return '''
    <h2>⏱️ Cronometraje Pro - Downhill MTB</h2>
    <p>Este sistema está en modo <strong>TIEMPO REAL</strong>.</p>
    <p>Para ver la pantalla en vivo, ve a: 
       <a href="/pantalla">/pantalla?event_code=TU_CODIGO</a>
    </p>
    <p>API activa en <code>/api/</code></p>
    '''

# === Pantalla en vivo (HTML embebido) — ✅ ESTÁNDARES UX/ISO PARA COMPETENCIA ===
@app.route('/pantalla')
def pantalla_vivo():
    return '''
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⏱️ Cronometraje en Vivo — Downhill MTB</title>
    <style>
        body {
            font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
            background: black;
            color: white;
            margin: 0;
            padding: 0;
            overflow-x: hidden;
        }
        .header {
            text-align: center;
            padding: 1rem;
            background: #1a202c;
            border-bottom: 2px solid #2b7a78;
        }
        .header h1 {
            font-size: 2.2rem;
            margin: 0;
            color: white;
            text-shadow: 0 0 10px rgba(43, 122, 120, 0.7);
        }
        .contador-maestro {
            font-size: 1.4rem;
            font-weight: bold;
            color: #68d391;
            margin-top: 0.5rem;
            font-family: 'Courier New', monospace;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
        }
        th, td {
            padding: 12px 8px;
            text-align: center;
            font-size: 1.3rem;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        th {
            background: #2b7a78;
            color: white;
            position: sticky;
            top: 0;
            font-weight: bold;
            font-size: 1.2rem;
        }
        .finalizado { background-color: #2d3748 !important; color: #68d391 !important; }
        .en-carrera { background-color: #2d3748 !important; color: #f6ad55 !important; }
        .sin-salida { background-color: #2d3748 !important; color: #fc8181 !important; }
        .fuera-lista { background-color: #4a5568 !important; color: #feb2b2 !important; }
        .estado { font-weight: bold; }
        tr:hover { opacity: 0.95; }
    </style>
</head>
<body>
    <div class="header">
        <h1>⏱️ CRONOMETRAJE EN VIVO -CronoAndes — Downhill MTB</h1>
        <div class="contador-maestro">⏰ Esperando primera salida...</div>
    </div>

    <table id="tabla-tiempos">
        <thead>
            <tr>
                <th>Pos</th>
                <th>Dorsal</th>
                <th>Nombre</th>
                <th>Categoría</th>
                <th>Tiempo</th>
            </tr>
        </thead>
        <tbody id="cuerpo-tabla"></tbody>
    </table>

    <script>
    document.addEventListener('DOMContentLoaded', function() {
        const urlParams = new URLSearchParams(window.location.search);
        let eventCode = urlParams.get('event_code');
        if (!eventCode) {
            eventCode = prompt("Ingresa el código del evento:");
            if (!eventCode) {
                document.body.innerHTML = '<div style="color:white;text-align:center;padding:4rem;font-size:1.5rem;background:black;">❌ Código requerido</div>';
                return;
            }
            window.history.replaceState(null, null, `?event_code=${encodeURIComponent(eventCode)}`);
        }

        const socket = io(window.location.origin, { transports: ['websocket'] });
        socket.emit('subscribe', { event_code: eventCode });

        let registros = {};
        let inscritos = {};
        let inicioOficial = null;

        function procesar(t) {
            if (!registros[t.dorsal]) registros[t.dorsal] = { salidas: [], llegadas: [] };
            if (t.action === 'salida') {
                registros[t.dorsal].salidas.push(t.timestamp);
                if (!inicioOficial) {
                    inicioOficial = new Date((t.timestamp.endsWith('Z') ? t.timestamp : t.timestamp + 'Z'));
                    actualizarContadorMaestro();
                    setInterval(actualizarContadorMaestro, 1000);
                }
            } else if (t.action === 'llegada') {
                registros[t.dorsal].llegadas.push(t.timestamp);
            }
            if (t.nombre && !inscritos[t.dorsal]) {
                inscritos[t.dorsal] = { dorsal: t.dorsal, nombre: t.nombre, categoria: t.categoria || '' };
            }
        }

        function calcularTiempo(dorsal) {
            const r = registros[dorsal] || { salidas: [], llegadas: [] };
            if (!r.salidas.length || !r.llegadas.length) return null;
            const s = new Date((r.salidas[0].endsWith('Z') ? r.salidas[0] : r.salidas[0] + 'Z'));
            const l = new Date((r.llegadas[0].endsWith('Z') ? r.llegadas[0] : r.llegadas[0] + 'Z'));
            if (isNaN(s) || isNaN(l) || l < s) return null;
            return Math.floor((l - s) / 1000);
        }

        function formatearDuracion(sec) {
            if (sec == null) return '';
            const h = Math.floor(sec / 3600);
            const m = Math.floor((sec % 3600) / 60);
            const s = sec % 60;
            return h > 0 
                ? `${h}:${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`
                : `${m}:${s.toString().padStart(2,'0')}`;
        }

        function actualizarContadorMaestro() {
            if (!inicioOficial) {
                document.querySelector('.contador-maestro').textContent = '⏰ Esperando primera salida...';
                return;
            }
            const ahora = new Date();
            const transcurrido = Math.floor((ahora - inicioOficial) / 1000);
            document.querySelector('.contador-maestro').textContent = `⏱️ En vivo: ${formatearDuracion(transcurrido)}`;
        }

        function renderizar() {
            const dorsales = [...new Set([...Object.keys(registros), ...Object.keys(inscritos)])];
            
            const finalizados = dorsales
                .map(d => {
                    const tiempo = calcularTiempo(d);
                    const insc = inscritos[d];
                    return {
                        dorsal: d,
                        nombre: insc ? insc.nombre : 'Fuera de lista',
                        categoria: insc ? insc.categoria : '',
                        tiempo: tiempo,
                        fuera: !insc
                    };
                })
                .filter(item => item.tiempo !== null)
                .sort((a, b) => {
                    if (a.tiempo !== b.tiempo) return a.tiempo - b.tiempo;
                    if (a.categoria !== b.categoria) return a.categoria.localeCompare(b.categoria);
                    return (parseInt(a.dorsal) || 0) - (parseInt(b.dorsal) || 0);
                });

            finalizados.forEach((item, i) => item.pos = i + 1);

            const filas = finalizados.map(f => `
                <tr class="${f.fuera ? 'fuera-lista' : 'finalizado'}">
                    <td>${f.pos}</td>
                    <td>${f.dorsal}${f.fuera ? ' 🚨' : ''}</td>
                    <td>${f.nombre}</td>
                    <td>${f.categoria}</td>
                    <td>${formatearDuracion(f.tiempo)}</td>
                </tr>
            `).join('');

            document.getElementById('cuerpo-tabla').innerHTML = filas || `
                <tr><td colspan="5" style="color:#a0aec0;">Esperando primeros tiempos...</td></tr>
            `;
        }

        Promise.all([
            fetch(`/api/inscritos/${encodeURIComponent(eventCode)}`).then(r => r.ok ? r.json() : []),
            fetch(`/api/tiempos/${encodeURIComponent(eventCode)}`).then(r => r.ok ? r.json() : [])
        ]).then(([inscritosData, tiemposData]) => {
            inscritos = {};
            inscritosData.forEach(p => inscritos[p.dorsal] = p);
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
        init_db()
        return jsonify({"status": "ok", "websocket_ready": True})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# === Iniciar ===
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)


