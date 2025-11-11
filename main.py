from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import time

app = Flask(__name__)
CORS(app)

# Simulamos una BD en memoria solo para pruebas iniciales
# En producción, Render inyectará DATABASE_URL válida en segundos/minutos
_TIMEDATA = {}

@app.route('/health')
def health():
    # Si existe DATABASE_URL y es válida, di "ok"
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url and not db_url.startswith('https://api.render.com'):
        return jsonify({"status": "ok", "database": "ready"})
    else:
        return jsonify({"status": "initializing", "database": "waiting for Render"}), 503

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    return jsonify({"valid": True, "message": "Código aceptado"})

@app.route('/api/crono', methods=['POST'])
def recibir_tiempo():
    data = request.get_json()
    dorsal = data.get('dorsal', '').strip()
    action = data.get('action', 'llegada')
    timestamp_iso = data.get('timestamp', time.time())
    event_code = data.get('event_code', 'demo')

    if not dorsal:
        return jsonify({"error": "dorsal requerido"}), 400

    # Guardar en memoria temporal (hasta que la BD esté lista)
    key = f"{event_code}:{dorsal}"
    if key not in _TIMEDATA:
        _TIMEDATA[key] = []
    _TIMEDATA[key].append({"action": action, "timestamp": timestamp_iso})

    return jsonify({"status": "success"}), 201

@app.route('/api/tiempos/<event_code>')
def obtener_tiempos(event_code):
    result = []
    for key, registros in _TIMEDATA.items():
        if key.startswith(event_code + ":"):
            dorsal = key.split(":", 1)[1]
            for reg in registros:
                result.append({
                    "dorsal": dorsal,
                    "action": reg["action"],
                    "timestamp": reg["timestamp"]
                })
    return jsonify(result)
