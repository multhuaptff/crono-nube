from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

def get_db_connection():
    # Render inyecta DATABASE_URL como: postgres://user:pass@host:port/db
    db_url = os.environ["DATABASE_URL"]
    # psycopg2 requiere "postgresql://", no "postgres://"
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url, sslmode='require')

@app.route('/health')
def health():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    return jsonify({"valid": True, "message": "Código aceptado"})

@app.route('/api/crono', methods=['POST'])
def recibir_tiempo():
    try:
        data = request.get_json()
        dorsal = data.get('dorsal', '').strip()
        action = data.get('action', 'llegada').strip().lower()
        timestamp_iso = data.get('timestamp', datetime.utcnow().isoformat())
        event_code = data.get('event_code', 'evento').strip()

        if not dorsal:
            return jsonify({"error": "dorsal requerido"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        
        # Crear tabla si no existe (idempotente)
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
        
        cur.execute(
            "INSERT INTO tiempos (evento, dorsal, action, timestamp_iso) VALUES (%s, %s, %s, %s)",
            (event_code, dorsal, action, timestamp_iso)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success"}), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/tiempos/<event_code>')
def obtener_tiempos(event_code):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT dorsal, action, timestamp_iso FROM tiempos WHERE evento = %s ORDER BY id", (event_code,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([
            {"dorsal": r[0], "action": r[1], "timestamp": r[2]} for r in rows
        ])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
