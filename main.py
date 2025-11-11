from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

@app.route('/api/crono', methods=['POST'])
def recibir_tiempo():
    try:
        data = request.get_json()
        dorsal = data.get('dorsal', '').strip()
        action = data.get('action', 'llegada').strip().lower()
        timestamp_iso = data.get('timestamp', datetime.utcnow().isoformat())
        event_code = data.get('event_code', 'default').strip()

        if not dorsal:
            return jsonify({"error": "dorsal requerido"}), 400

        DATABASE_URL = os.environ.get('DATABASE_URL')
        if not DATABASE_URL:
            return jsonify({"error": "Base de datos no configurada"}), 500

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS tiempos (
                id SERIAL PRIMARY KEY,
                evento TEXT NOT NULL,
                dorsal TEXT NOT NULL,
                action TEXT NOT NULL,
                timestamp_iso TEXT NOT NULL
            )
        ''')
        cur.execute(
            "INSERT INTO tiempos (evento, dorsal, action, timestamp_iso) VALUES (%s, %s, %s, %s)",
            (event_code, dorsal, action, timestamp_iso)
        )
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"ok": True, "dorsal": dorsal, "action": action}), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/tiempos/<event_code>')
def obtener_tiempos(event_code):
    try:
        DATABASE_URL = os.environ.get('DATABASE_URL')
        if not DATABASE_URL:
            return jsonify([]), 500

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT dorsal, action, timestamp_iso FROM tiempos WHERE evento = %s ORDER BY id DESC", (event_code,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        return jsonify([
            {"dorsal": r[0], "action": r[1], "timestamp": r[2]} for r in rows
        ])
    except:
        return jsonify([])

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))