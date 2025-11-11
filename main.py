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

@app.route('/health')
def health():
    try:
        db_url = os.environ.get('DATABASE_URL', '').strip()
        if not db_url:
            return jsonify({"status": "no DATABASE_URL"}), 503
        if db_url.startswith('https://api.render.com'):
            return jsonify({"status": "waiting for DB"}), 503
        init_db()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 503

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    # Siempre válido por ahora
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
