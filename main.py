from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get('DATABASE_URL')

def init_db():
    """Crea la tabla si no existe."""
    if not DATABASE_URL:
        raise Exception("DATABASE_URL no está configurada")
    conn = psycopg2.connect(DATABASE_URL)
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

# ✅ Asegurar que la BD esté lista al iniciar la app (importante en Render)
with app.app_context():
    init_db()

@app.route('/health')
def health():
    return jsonify({"status": "ok", "database": "connected"})

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    try:
        data = request.get_json()
        code = data.get('code', '').strip().upper()
        if code and len(code) >= 3:
            return jsonify({"valid": True, "message": "Código correcto"})
        return jsonify({"valid": False, "message": "Código incorrecto"}), 400
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 400

@app.route('/api/crono', methods=['POST'])
def recibir_tiempo():
    try:
        data = request.get_json()
        dorsal = data.get('dorsal', '').strip()
        action = data.get('action', 'llegada').strip().lower()
        timestamp_iso = data.get('timestamp', datetime.utcnow().isoformat())
        event_code = data.get('event_code', 'evento_default').strip()

        if not dorsal:
            return jsonify({"error": "dorsal requerido"}), 400

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tiempos (evento, dorsal, action, timestamp_iso) VALUES (%s, %s, %s, %s)",
            (event_code, dorsal, action, timestamp_iso)
        )
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"status": "success", "dorsal": dorsal, "action": action}), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/tiempos/<event_code>')
def obtener_tiempos(event_code):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT dorsal, action, timestamp_iso FROM tiempos WHERE evento = %s ORDER BY creado_en ASC", (event_code,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        tiempos = [
            {"dorsal": r[0], "action": r[1], "timestamp": r[2]} for r in rows
        ]
        return jsonify(tiempos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
