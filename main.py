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

# === Pantalla en vivo (HTML embebido) — ✅ CORREGIDO Y COMPLETO PARA RENDER ===
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
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: white; color: #2d3748; padding: 1rem; }
        .header { text-align: center; margin-bottom: 1.5rem; }
        .metrics { display: flex; gap: 1rem; margin-bottom: 1rem; font-size: 1.1em; flex-wrap: wrap; }
        .metric { background: #f8f9fa; padding: 0.5rem 1rem; border-radius: 6px; min-width: 120px; text-align: center; }
        table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #e2e8f0; }
        th { background: #2b7a78; color: white; position: sticky; top: 0; }
        .finalizado { background-color: #d4edda !important; }
        .en-carrera { background-color: #fff3cd !important; }
        .sin-salida { background-color: #f8d7da !important; }
        .fuera-lista { background-color: #ffebee !important; }
        .estado { font-weight: bold; }
        .contador { color: #e53e3e; font-weight: bold; margin-top: 0.5rem; }
        .error { color: #c53030; background: #fed7d7; padding: 0.5rem; border-radius: 4px; margin: 0.5rem 0; }
        @media (max-width: 768px) {
            table, thead, tbody, th, td, tr { display: block; }
            thead tr { position: absolute; top: -9999px; left: -9999px; }
            tr { border: 1px solid #ccc; margin-bottom: 10px; }
            td { border: none; position: relative; padding-left: 50%; text-align: right; }
            td:before { content: attr(data-label); position: absolute; left: 10px; width: 45%; text-align: left; font-weight: bold; }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>⏱️ Cronometraje en Vivo — Downhill MTB</h1>
        <p><strong id="evento">Cargando...</strong> | Código: <code id="codigo">-</code></p>
        <p class="contador">🔄 Última actualización: <span id="ultima">-</span></p>
        <div id="error-container"></div>
    </div>

    <div class="metrics">
        <div class="metric">Total: <strong id="total">0</strong></div>
        <div class="metric">🏁 Finalizados: <strong id="finalizados">0</strong></div>
        <div class="metric">🚴 En carrera: <strong id="en_carrera">0</strong></div>
    </div>

    <table id="tabla-tiempos">
        <thead>
            <tr>
                <th>Estado</th>
                <th>Dorsal</th>
                <th>Nombre</th>
                <th>Categoría</th>
                <th>Salida</th>
                <th>Llegada</th>
                <th>Total</th>
            </tr>
        </thead>
        <tbody id="cuerpo-tabla"></tbody>
    </table>

    <script>
    document.addEventListener('DOMContentLoaded', function() {
        function showError(msg) {
            document.getElementById('error-container').innerHTML = `<div class="error">⚠️ ${msg}</div>`;
        }

        function getEventCodeFromUrl() {
            const match = window.location.search.match(/event_code=([^&]*)/);
            return match ? decodeURIComponent(match[1]) : null;
        }

        let eventCode = getEventCodeFromUrl();
        if (!eventCode) {
            eventCode = prompt("Ingresa el código del evento:");
            if (!eventCode) {
                alert("Código requerido.");
                location.href = "/";
            } else {
                history.replaceState(null, null, `?event_code=${encodeURIComponent(eventCode)}`);
            }
        }
        document.getElementById('codigo').textContent = eventCode;

        const socket = io(window.location.origin, {
            transports: ['websocket'],
            autoConnect: true
        });
        socket.emit('subscribe', {event_code: eventCode});

        let registros = {};
        let inscritos = {};

        function procesar(t) {
            if (!registros[t.dorsal]) registros[t.dorsal] = {salidas:[], llegadas:[]};
            if (t.action === 'salida') registros[t.dorsal].salidas.push(t.timestamp);
            else if (t.action === 'llegada') registros[t.dorsal].llegadas.push(t.timestamp);
            if (t.nombre && !inscritos[t.dorsal]) {
                inscritos[t.dorsal] = {dorsal:t.dorsal, nombre:t.nombre, categoria:t.categoria||''};
            }
        }

        function estado(dorsal) {
            const r = registros[dorsal] || {salidas:[], llegadas:[]};
            if (r.salidas.length && r.llegadas.length) return 'Finalizado';
            if (r.salidas.length) return 'En carrera';
            return 'Sin salida';
        }

        function tiempos(dorsal) {
            const r = registros[dorsal] || {salidas:[], llegadas:[]};
            if (!r.salidas.length || !r.llegadas.length) 
                return {salida:'', llegada:'', total:''};

            const sStr = r.salidas[0].endsWith('Z') ? r.salidas[0] : r.salidas[0] + 'Z';
            const lStr = r.llegadas[0].endsWith('Z') ? r.llegadas[0] : r.llegadas[0] + 'Z';
            const s = new Date(sStr);
            const l = new Date(lStr);
            if (isNaN(s.getTime()) || isNaN(l.getTime()))
                return {salida:'', llegada:'', total:''};

            const totalSec = Math.floor((l - s) / 1000);
            if (totalSec < 0) return {salida:'', llegada:'', total:'⚠️'};

            const fmt = (d) => {
                const pad = (n) => n.toString().padStart(2, '0');
                return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
            };
            const fmtDur = (sec) => {
                const h = Math.floor(sec / 3600);
                const m = Math.floor((sec % 3600) / 60);
                const s = sec % 60;
                return h ? `${h}:${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}` 
                         : `${m}:${s.toString().padStart(2,'0')}`;
            };
            return {salida: fmt(s), llegada: fmt(l), total: fmtDur(totalSec)};
        }

        function renderizar() {
            try {
                const dorsales = [...new Set([...Object.keys(registros), ...Object.keys(inscritos)])];
                const filas = dorsales.map(d => {
                    const insc = inscritos[d];
                    const est = estado(d);
                    const t = tiempos(d);
                    const fuera = !insc;
                    return {
                        d, nombre: fuera ? 'Fuera de lista' : (insc.nombre||''),
                        cat: fuera ? '' : (insc.categoria||''),
                        est, ...t, fuera
                    };
                }).sort((a,b) => {
                    const ord = {'Sin salida':0, 'En carrera':1, 'Finalizado':2};
                    return (ord[a.est]-ord[b.est]) || a.cat.localeCompare(b.cat) || a.d.localeCompare(b.d,undefined,{numeric:true});
                });

                document.getElementById('cuerpo-tabla').innerHTML = filas.map(f => `
                    <tr class="${f.fuera?'fuera-lista':f.est==='Finalizado'?'finalizado':f.est==='En carrera'?'en-carrera':'sin-salida'}">
                        <td data-label="Estado" class="estado">${f.est}</td>
                        <td data-label="Dorsal">${f.d}${f.fuera?' 🚨':''}</td>
                        <td data-label="Nombre">${f.nombre}</td>
                        <td data-label="Categoría">${f.cat}</td>
                        <td data-label="Salida">${f.salida}</td>
                        <td data-label="Llegada">${f.llegada}</td>
                        <td data-label="Total">${f.total}</td>
                    </tr>
                `).join('');

                const total = filas.length;
                const fin = filas.filter(f => f.est === 'Finalizado').length;
                document.getElementById('total').textContent = total;
                document.getElementById('finalizados').textContent = fin;
                document.getElementById('en_carrera').textContent = total - fin;
                document.getElementById('evento').textContent = `Evento: ${eventCode} (${total} corredores)`;
            } catch (e) {
                console.error("Error en renderizar:", e);
                document.getElementById('cuerpo-tabla').innerHTML = `<tr><td colspan="7">Error al mostrar resultados</td></tr>`;
            }
        }

        fetch(`/api/inscritos/${encodeURIComponent(eventCode)}`)
            .then(r => r.ok ? r.json() : [])
            .then(data => {
                inscritos = {};
                data.forEach(p => inscritos[p.dorsal] = p);
                renderizar();
            })
            .catch(() => renderizar());

        fetch(`/api/tiempos/${encodeURIComponent(eventCode)}`)
            .then(r => {
                if (!r.ok) throw new Error('API no disponible');
                return r.json();
            })
            .then(tiempos => {
                tiempos.forEach(t => procesar(t));
                renderizar();
            })
            .catch(err => {
                console.error("Error al cargar tiempos:", err);
                showError("No se pudieron cargar los tiempos. Verifica el código del evento.");
            });

        socket.on('nuevo_tiempo', (d) => {
            procesar(d);
            document.getElementById('ultima').textContent = new Date().toLocaleTimeString();
            renderizar();
        });

        socket.on('connect', () => {
            document.getElementById('ultima').textContent = 'Conectado';
        });

        socket.on('connect_error', () => {
            showError("Modo offline: los nuevos tiempos no se mostrarán en tiempo real.");
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
