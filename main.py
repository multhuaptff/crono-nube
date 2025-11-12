from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import psycopg2
from datetime import datetime

app = Flask(__name__)
CORS(app)

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
    conn.commit()
    cur.close()
    conn.close()

def init_db_inscritos():
    """Inicializa la tabla de inscritos para sincronización con Streamlit"""
    conn = get_db_conn()
    cur = conn.cursor()
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
    cur.execute('''
        CREATE INDEX IF NOT EXISTS idx_inscritos_event ON inscritos (event_code)
    ''')
    conn.commit()
    cur.close()
    conn.close()

@app.route('/health')
def health():
    try:
        db_url = os.environ.get('DATABASE_URL', '').strip()
        if not db_url:
            return jsonify({"status": "no DATABASE_URL"}), 503
        if db_url.startswith('https://api.render.com'):
            return jsonify({"status": "waiting for DB"}), 503
        init_db()
        init_db_inscritos()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 503

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    return jsonify({"valid": True})

@app.route('/api/crono', methods=['POST'])
def crono():
    try:
        db_url = os.environ.get('DATABASE_URL', '').strip()
        if not db_url or db_url.startswith('https://api.render.com'):
            return jsonify({"error": "base de datos no lista"}), 503

        init_db()
        data = request.get_json()
        if not data:
            return jsonify({"error": "JSON inválido"}), 400

        dorsal = data.get('dorsal', '').strip()
        action = data.get('action', 'llegada').strip().lower()
        ts = data.get('timestamp', datetime.utcnow().isoformat()).strip()
        event = data.get('event_code', 'demo').strip()

        if not dorsal:
            return jsonify({"error": "dorsal requerido"}), 400

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tiempos (evento, dorsal, action, timestamp_iso) VALUES (%s, %s, %s, %s)",
            (event, dorsal, action, ts)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/tiempos/<event>')
def tiempos(event):
    try:
        db_url = os.environ.get('DATABASE_URL', '').strip()
        if not db_url or db_url.startswith('https://api.render.com'):
            return jsonify([])

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT dorsal, action, timestamp_iso FROM tiempos WHERE evento = %s ORDER BY id", (event,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([
            {"dorsal": r[0], "action": r[1], "timestamp": r[2]} for r in rows
        ])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === ENDPOINT: Recibir y servir lista de inscritos ===
@app.route('/api/inscritos/<event_code>', methods=['POST'])
def recibir_inscritos(event_code):
    """Recibe lista de inscritos desde Streamlit (Inscripción)"""
    try:
        db_url = os.environ.get('DATABASE_URL', '').strip()
        if not db_url or db_url.startswith('https://api.render.com'):
            return jsonify({"error": "base de datos no lista"}), 503

        init_db_inscritos()
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
                ''', (event_code.strip(), dorsal, nombre, categoria, club, rfid))
                count += 1
        
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success", "count": count}), 201
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/inscritos/<event_code>', methods=['GET'])
def obtener_inscritos(event_code):
    """Devuelve lista de inscritos para Streamlit (Lista de Salida) y app móvil"""
    try:
        db_url = os.environ.get('DATABASE_URL', '').strip()
        if not db_url or db_url.startswith('https://api.render.com'):
            return jsonify([])

        init_db_inscritos()
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute('''
            SELECT dorsal, nombre, categoria, club, rfid 
            FROM inscritos 
            WHERE event_code = %s 
            ORDER BY dorsal
        ''', (event_code.strip(),))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        return jsonify([
            {
                "dorsal": r[0],
                "nombre": r[1],
                "categoria": r[2],
                "club": r[3],
                "rfid": r[4]
            } for r in rows
        ])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === ENDPOINT: Eliminar evento en la nube ===
@app.route('/api/flush-event/<event_code>', methods=['DELETE'])
def flush_event(event_code):
    """Elimina TODOS los tiempos de un evento (solo para pruebas/organización)."""
    try:
        if not event_code or event_code.strip() == "":
            return jsonify({"error": "event_code requerido"}), 400

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM tiempos WHERE evento = %s", (event_code.strip(),))
        count = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({
            "status": "success",
            "deleted": count,
            "event_code": event_code
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
