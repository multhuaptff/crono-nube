from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
import os
from datetime import datetime
from urllib.parse import urlparse

app = Flask(__name__)
CORS(app)

def get_db_connection():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL no está configurada")
    
    # Parsear manualmente la URL de PostgreSQL
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    
    if not database_url.startswith("postgresql://"):
        # No es una URL de BD válida → Render puede estar en transición
        raise Exception("DATABASE_URL no es una URL de PostgreSQL válida")
    
    parsed = urlparse(database_url)
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port,
        dbname=parsed.path[1:],  # elimina la primera barra
        user=parsed.username,
        password=parsed.password,
        sslmode='require'
    )
    return conn

def ensure_table_exists():
    conn = get_db_connection()
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
        ensure_table_exists()
        return jsonify({"status": "ok", "database": "connected"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

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
        ensure_table_exists()
        data = request.get_json()
        dorsal = data.get('dorsal', '').strip()
        action = data.get('action', 'llegada').strip().lower()
        timestamp_iso = data.get('timestamp', datetime.utcnow().isoformat())
        event_code = data.get('event_code', 'evento_default').strip()

        if not dorsal:
            return jsonify({"error": "dorsal requerido"}), 400

        conn = get_db_connection()
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
        ensure_table_exists()
        conn = get_db_connection()
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
