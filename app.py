#!/usr/bin/env python3
"""
app.py — WebApp Flask per editare GPX con preview real-time.

Serve una webapp che permette di:
- Caricare file GPX
- Visualizzare mappa e altimetria in real-time
- Regolare smoothing con slider
- Modificare manualmente distanza e dislivello
- Esportare i dati

Uso:
    python generator/app.py
    Accedi a http://localhost:5000
"""

import json
import math
import time
import threading
import webbrowser
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
from flask import Flask, render_template, request, jsonify, Response
from werkzeug.utils import secure_filename

# ── COSTANTI ──────────────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {'gpx'}
UPLOAD_FOLDER = Path(__file__).parent.parent / 'temp_gpx'
UPLOAD_FOLDER.mkdir(exist_ok=True)

# ── UTILITY FUNCTIONS ─────────────────────────────────────────────────────────

def parse_gpx(gpx_path: Path, smoothing_window: int = 0) -> dict:
    """Estrae distanza (km), dislivello positivo (m) e punti GPX dal file GPX.
    
    Args:
        gpx_path: Path al file GPX
        smoothing_window: Finestra di smoothing per le quote (default 0 = GPX originale)
    """
    try:
        tree = ET.parse(gpx_path)
        root = tree.getroot()
        ns = ''
        if root.tag.startswith('{'):
            ns = root.tag.split('}')[0] + '}'

        points = root.findall(f'.//{ns}trkpt')
        if not points:
            points = root.findall(f'.//{ns}rtept')

        if not points:
            return {'distanza_km': None, 'dislivello_m': None, 'gpx_points': None}

        coords = []
        gpx_points = []
        for pt in points:
            try:
                lat = float(pt.get('lat'))
                lon = float(pt.get('lon'))
                ele_el = pt.find(f'{ns}ele')
                ele = float(ele_el.text) if ele_el is not None else None
                coords.append((lat, lon, ele))
                gpx_points.append({
                    'lat': round(lat, 6),
                    'lon': round(lon, 6),
                    'ele': round(ele, 1) if ele is not None else None
                })
            except (TypeError, ValueError):
                continue

        if not coords:
            return {'distanza_km': None, 'dislivello_m': None, 'gpx_points': None}

        def haversine(lat1, lon1, lat2, lon2):
            R = 6371000
            φ1, φ2 = math.radians(lat1), math.radians(lat2)
            dφ = math.radians(lat2 - lat1)
            dλ = math.radians(lon2 - lon1)
            a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
            return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        dist_m = sum(
            haversine(coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1])
            for i in range(len(coords)-1)
        )

        # Smoothing quote: preserva la lunghezza completa dei punti.
        # Se un punto non ha elevazione, propaga l'ultimo valore valido.
        ele_series = []
        last_ele = None
        for _, _, ele in coords:
            if ele is not None:
                last_ele = ele
                ele_series.append(ele)
            else:
                ele_series.append(last_ele if last_ele is not None else 0.0)

        w = max(0, int(smoothing_window))
        if w <= 1:
            eles = ele_series[:]  # base GPX senza smoothing
        else:
            eles = []
            for i in range(len(ele_series)):
                start = max(0, i - w // 2)
                end = min(len(ele_series), i + w // 2 + 1)
                window_vals = ele_series[start:end]
                eles.append(sum(window_vals) / len(window_vals))

        # Applica la quota smussata ai punti restituiti al frontend.
        for i, p in enumerate(gpx_points):
            p['ele'] = round(eles[i], 1)

        d_plus = 0.0
        for i in range(1, len(eles)):
            diff = eles[i] - eles[i-1]
            if diff > 0:
                d_plus += diff

        finish = coords[-1]
        center_lat, center_lon = finish[0], finish[1]

        return {
            'distanza_km': round(dist_m / 1000, 2),
            'dislivello_m': round(d_plus) if d_plus > 0 else None,
            'gpx_points': gpx_points,
            'center_lat': center_lat,
            'center_lon': center_lon,
        }

    except Exception as e:
        return {'error': str(e), 'distanza_km': None, 'dislivello_m': None, 'gpx_points': None}


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Reverse geocoding tramite Nominatim"""
    try:
        params = urllib.parse.urlencode({
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "format": "json",
            "zoom": 8,
            "addressdetails": 1,
        })
        url = f"https://nominatim.openstreetmap.org/reverse?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "race-db-archivio/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        addr = data.get("address", {})
        provincia = (
            addr.get("county") or
            addr.get("city") or
            addr.get("town") or
            addr.get("village") or
            ""
        )
        for prefix in ("Provincia di ", "Province of ", "Distretto di "):
            if provincia.startswith(prefix):
                provincia = provincia[len(prefix):]

        country_code = addr.get("country_code", "").upper()
        parts = [p for p in [provincia, country_code] if p]
        return ", ".join(parts) if parts else None
    except Exception:
        return None


# ── FLASK APP ─────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder=Path(__file__).parent)
app.config['UPLOAD_FOLDER'] = str(UPLOAD_FOLDER)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

# Session storage per GPX caricati
gpx_cache = {}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def index():
    """Pagina principale"""
    return render_template('edita_gpx.html')


@app.route('/api/upload', methods=['POST'])
def upload():
    """Carica un file GPX e ritorna i dati iniziali"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'Nessun file'}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'File vuoto'}), 400

        if not allowed_file(file.filename):
            return jsonify({'error': 'Solo file GPX'}), 400

        # Salva file temporaneamente
        filename = secure_filename(file.filename)
        timestamp = str(int(datetime.now().timestamp()))
        session_id = f"{timestamp}_{filename}"
        filepath = Path(app.config['UPLOAD_FOLDER']) / session_id

        file.save(str(filepath))

        # Parsa il GPX
        gpx_data = parse_gpx(filepath, smoothing_window=0)

        if gpx_data.get('error'):
            return jsonify({'error': gpx_data['error']}), 400

        if not gpx_data.get('gpx_points'):
            return jsonify({'error': 'File GPX non valido'}), 400

        # Salva in cache
        gpx_cache[session_id] = {
            'filepath': str(filepath),
            'filename': filename,
            'data': gpx_data
        }

        # Reverse geocoding
        luogo = None
        if gpx_data.get('center_lat') and gpx_data.get('center_lon'):
            luogo = reverse_geocode(gpx_data['center_lat'], gpx_data['center_lon'])

        return jsonify({
            'session_id': session_id,
            'filename': filename,
            'distanza_km': gpx_data.get('distanza_km'),
            'dislivello_m': gpx_data.get('dislivello_m'),
            'gpx_points': gpx_data.get('gpx_points'),
            'luogo': luogo,
        })

    except Exception as e:
        return jsonify({'error': f'Errore: {str(e)}'}), 500


@app.route('/api/smooth/<session_id>/<int:window>', methods=['GET'])
def smooth(session_id, window):
    """Ricalcola i dati con diverso livello di smoothing"""
    try:
        if session_id not in gpx_cache:
            return jsonify({'error': 'Sessione non trovata'}), 404

        filepath = gpx_cache[session_id]['filepath']
        gpx_data = parse_gpx(Path(filepath), smoothing_window=window)

        if gpx_data.get('error'):
            return jsonify({'error': gpx_data['error']}), 400

        return jsonify({
            'distanza_km': gpx_data.get('distanza_km'),
            'dislivello_m': gpx_data.get('dislivello_m'),
            'gpx_points': gpx_data.get('gpx_points'),
        })

    except Exception as e:
        return jsonify({'error': f'Errore: {str(e)}'}), 500


@app.route('/api/export/<session_id>', methods=['POST'])
def export_data(session_id):
    """Esporta i dati finali"""
    try:
        if session_id not in gpx_cache:
            return jsonify({'error': 'Sessione non trovata'}), 404

        data = request.get_json()
        
        filename = gpx_cache[session_id]['filename']
        export = {
            'slug': Path(filename).stem,
            'distanza_km': data.get('distanza_km'),
            'dislivello_m': data.get('dislivello_m'),
            'gpx_points': data.get('gpx_points'),
        }

        return jsonify(export)

    except Exception as e:
        return jsonify({'error': f'Errore: {str(e)}'}), 500


@app.route('/api/export-gpx/<session_id>', methods=['POST'])
def export_gpx(session_id):
    """Esporta un file GPX con quote aggiornate dal frontend."""
    try:
        if session_id not in gpx_cache:
            return jsonify({'error': 'Sessione non trovata'}), 404

        data = request.get_json() or {}
        edited_points = data.get('gpx_points') or []
        if not edited_points:
            return jsonify({'error': 'Nessun punto GPX da esportare'}), 400

        filepath = Path(gpx_cache[session_id]['filepath'])
        filename = gpx_cache[session_id]['filename']

        tree = ET.parse(filepath)
        root = tree.getroot()
        ns = ''
        if root.tag.startswith('{'):
            ns = root.tag.split('}')[0] + '}'

        xml_points = root.findall(f'.//{ns}trkpt')
        if not xml_points:
            xml_points = root.findall(f'.//{ns}rtept')

        if not xml_points:
            return jsonify({'error': 'GPX sorgente non valido'}), 400

        limit = min(len(xml_points), len(edited_points))
        for i in range(limit):
            pt = xml_points[i]
            ele_value = edited_points[i].get('ele')
            if ele_value is None:
                continue

            ele_el = pt.find(f'{ns}ele')
            if ele_el is None:
                ele_el = ET.SubElement(pt, f'{ns}ele')
            ele_el.text = f"{float(ele_value):.1f}"

        gpx_text = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        out_name = f"{Path(filename).stem}-edited.gpx"

        return Response(
            gpx_text,
            mimetype='application/gpx+xml',
            headers={
                'Content-Disposition': f'attachment; filename="{out_name}"'
            }
        )

    except Exception as e:
        return jsonify({'error': f'Errore: {str(e)}'}), 500


if __name__ == '__main__':
    _browser_opened = False
    
    def open_browser():
        """Apri il browser dopo che il server è avviato"""
        global _browser_opened
        if _browser_opened:
            return
        _browser_opened = True
        time.sleep(2)
        try:
            webbrowser.open("http://localhost:5000")
            print("✓ Browser aperto")
        except Exception:
            pass
    
    print("🚀 Avvio web app...")
    print("📍 Server: http://localhost:5000")
    print("⏹ Premi Ctrl+C per arresto\n")
    
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()
    
    app.run(host='localhost', port=5000, use_reloader=False)
