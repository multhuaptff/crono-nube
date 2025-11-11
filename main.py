# main.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# === Obtener DATABASE_URL desde Render ===
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise Exception("❌ Error: DATABASE_URL no está configurada. Ve a Render → Environment y agrégala.")

# === Función segura para inicializar la BD (solo una vez) ===
_db_initialized = False

def init_db():
    global _db_initialized
    if _db_initialized:
        return
    try:
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
        _db_initialized = True
        print("✅ Tabla 'tiempos' verificada/creada")
    except Exception as e:
        print(f"❌ Error al inicializar BD: {e}")
        raise

# === Rutas ===
@app.route('/health')
def health():
    init_db()  # Asegura que la BD funcione
    return jsonify({"status": "ok", "database": "connected", "url": DATABASE_URL[:30] + "..."})

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    # ✅ Siempre acepta cualquier código (puedes mejorar esto después)
    return jsonify({"valid": True, "message": "Código aceptado"})

@app.route('/api/crono', methods=['POST'])
def recibir_tiempo():
    init_db()
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Cuerpo JSON vacío"}), 400

        dorsal = str(data.get('dorsal', '')).strip()
        action = str(data.get('action', 'llegada')).strip().lower()
        timestamp_iso = str(data.get('timestamp', datetime.utcnow().isoformat())).strip()
        event_code = str(data.get('event_code', 'evento')).strip()[:10]

        if not dorsal:
            return jsonify({"error": "dorsal requerido"}), 400
        if action not in ['salida', 'llegada']:
            return jsonify({"error": "action debe ser 'salida' o 'llegada'"}), 400

        # Guardar en PostgreSQL
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tiempos (evento, dorsal, action, timestamp_iso) VALUES (%s, %s, %s, %s)",
            (event_code, dorsal, action, timestamp_iso)
        )
        conn.commit()
        cur.close()
        conn.close()

        print(f"✅ Registrado: {dorsal} - {action} - {event_code}")
        return jsonify({"status": "success", "dorsal": dorsal, "action": action}), 201

    except Exception as e:
        print(f"❌ Error en /api/crono: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/tiempos/<event_code>')
def obtener_tiempos(event_code):
    init_db()
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT dorsal, action, timestamp_iso 
            FROM tiempos 
            WHERE evento = %s 
            ORDER BY creado_en ASC
        """, (event_code,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        tiempos = [
            {"dorsal": r[0], "action": r[1], "timestamp": r[2]} for r in rows
        ]
        return jsonify(tiempos)
    except Exception as e:
        print(f"❌ Error en /api/tiempos: {e}")
        return jsonify({"error": str(e)}), 500

# === No se llama init_db() aquí porque Render usa Gunicorn (no __main__) ===
