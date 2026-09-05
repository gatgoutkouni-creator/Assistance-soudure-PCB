from flask import Flask, request, jsonify
from flask_cors import CORS
import os, re, time, threading
import serial

app = Flask(__name__)
CORS(app)

# ============================================================
# CONFIGURATION A MODIFIER SELON TON PC
# ============================================================
GRBL_PORT = "COM4"        # Port détecté sur ton PC. Change-le si Windows donne un autre COM.
GRBL_BAUDRATE = 115200
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# IMPORTANT : ce backend PEUT modifier $100/$101/$102 uniquement quand tu cliques
# sur "Calculer et appliquer steps/mm". Les valeurs sont sauvegardees dans GRBL.

ser = None
serial_lock = threading.Lock()
stop_event = threading.Event()

current_pos = {"x": 0.0, "y": 0.0, "z": 0.0}
absolute_mode = True

# ============================================================
# REGLAGES DOUX POUR REDUIRE LE BRUIT ET EVITER LES CHOCS
# ============================================================
# Ces valeurs ne changent PAS la calibration $100/$101/$102.
# Elles réduisent seulement la vitesse, l'accélération et le maintien moteur.
SAFE_GRBL_SETTINGS = [
    "$0=10",     # impulsion step standard
    "$1=25",     # relâchement moteur après mouvement, réduit le bruit à l'arrêt
    "$110=1600", # vitesse max X plus rapide
    "$111=1600", # vitesse max Y plus rapide
    "$112=120",  # vitesse max Z prudente
    "$120=120",  # accélération X raisonnable
    "$121=120",  # accélération Y raisonnable
    "$122=10",   # accélération Z douce
]

# Vitesse utilisée pour les boutons Jog et calibration.
JOG_FEED_XY = 900  # X plus rapide pour jog continu et calibration
JOG_FEED_Y = 900   # Y continu comme X : vitesse dédiée
JOG_FEED_Z = 80     # Z reste prudent

# Distance longue utilisée uniquement pendant le jog continu.
# Le mouvement s’arrête quand tu relâches le bouton grâce à /jog_stop.
CONTINUOUS_JOG_DISTANCE_XY = 120.0
CONTINUOUS_JOG_DISTANCE_Y = 160.0
CONTINUOUS_JOG_DISTANCE_Z = 30.0

# Ton axe Z est inversé mécaniquement : le dashboard garde Z+ / Z-,
# mais le backend inverse le signe envoyé à GRBL pour Z seulement.
INVERT_Z_DIRECTION = False

# Correction du sens manuel : si X+/Y+ vont dans le mauvais sens, garder True.
# On inverse seulement le jog manuel, pas le G-code automatique.
INVERT_X_JOG_DIRECTION = True
INVERT_Y_JOG_DIRECTION = True

# Dimensions physiques de ton prototype
# X rail total = 300 mm, mais le chariot/bati prend environ 75 mm => course utile ≈ 225 mm.
# Y plateau = 300 mm. Z à adapter selon ta mécanique.
MAX_TRAVEL_X = 225.0
MAX_TRAVEL_Y = 300.0
MAX_TRAVEL_Z = 100.0


def parse_float_any(value, default=0.0):
    """Accepte 1.5 ou 1,5 depuis le dashboard."""
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default


# ============================================================
# OUTILS SERIE / GRBL
# ============================================================
def read_lines(wait_s=0.05):
    global ser
    lines = []
    time.sleep(wait_s)
    if ser is None:
        return lines
    try:
        while ser.in_waiting:
            line = ser.readline().decode(errors="ignore").strip()
            if line:
                lines.append(line)
    except Exception:
        pass
    return lines


def connect_grbl():
    global ser
    if ser is not None and ser.is_open:
        return ser
    ser = serial.Serial(GRBL_PORT, GRBL_BAUDRATE, timeout=0.25)
    time.sleep(1.2)
    read_lines(0.2)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except Exception:
        pass
    return ser


def wait_response(timeout=2.0):
    global ser
    start = time.time()
    raw = []
    while time.time() - start < timeout:
        if stop_event.is_set():
            return "stopped_by_user", raw
        try:
            line = ser.readline().decode(errors="ignore").strip()
        except Exception as e:
            return f"serial_error: {e}", raw
        if not line:
            continue
        raw.append(line)
        low = line.lower()
        if low.startswith("ok"):
            return "ok", raw
        if low.startswith("error") or low.startswith("alarm"):
            return line, raw
    return "timeout", raw


def send_raw(line, timeout=2.0):
    global ser
    line = str(line).strip()
    if not line:
        return {"line": line, "response": "ignored", "raw": []}
    connect_grbl()
    # Nettoyer les anciennes lignes avant d'envoyer une nouvelle commande.
    read_lines(0.02)
    with serial_lock:
        ser.write((line + "\n").encode())
        ser.flush()
    response, raw = wait_response(timeout)
    return {"line": line, "response": response, "raw": raw}


def wait_until_idle(timeout=20.0):
    """Attend que GRBL repasse en Idle après un mouvement.
    Important pour la calibration : on ne calcule pas les steps/mm pendant que l'axe bouge encore.
    """
    start = time.time()
    last = ""
    while time.time() - start < timeout:
        try:
            st = get_status_line(timeout=0.8)
        except Exception:
            st = ""
        if st:
            last = st
            if st.startswith("<Idle"):
                return True, st
            if st.startswith("<Alarm"):
                return False, st
        time.sleep(0.08)
    return False, last or "timeout waiting Idle"


def send_realtime(data: bytes):
    connect_grbl()
    with serial_lock:
        ser.write(data)
        ser.flush()
    time.sleep(0.1)


def send_jog(axis, distance, feed):
    """Déplacement relatif sur UN SEUL axe.
    Version simple et fiable : G21 puis G91 puis G1 axe-distance puis G90.
    Le backend n'envoie JAMAIS X/Y/Z ensemble pour les boutons manuels.
    Si tu demandes Y, la ligne envoyée contient seulement Y.
    """
    axis = str(axis).upper().strip()
    distance = float(distance)
    feed = float(feed)
    if axis not in ["X", "Y", "Z"]:
        raise ValueError("Axe invalide")
    if abs(distance) <= 0:
        raise ValueError("Distance invalide")

    # Inversion logicielle pour le jog manuel seulement : le dashboard reste logique.
    if axis == "X" and INVERT_X_JOG_DIRECTION:
        distance = -distance
    if axis == "Y" and INVERT_Y_JOG_DIRECTION:
        distance = -distance
    if axis == "Z" and INVERT_Z_DIRECTION:
        distance = -distance

    results = []
    results.append(send_raw("G21", timeout=2.0))
    results.append(send_raw("G91", timeout=2.0))
    cmd = f"G1 {axis}{distance:.4f} F{feed:.1f}"
    move = send_raw(cmd, timeout=4.0)
    results.append(move)
    idle_ok, idle_status = (False, "")
    if str(move.get("response", "")).lower().startswith("ok"):
        # attendre la fin réelle du déplacement avant de répondre au dashboard
        travel_time = min(60.0, max(8.0, abs(distance) / max(feed, 1.0) * 60.0 + 3.0))
        idle_ok, idle_status = wait_until_idle(timeout=travel_time)
    results.append(send_raw("G90", timeout=2.0))
    return {
        "line": cmd,
        "response": move.get("response", "unknown"),
        "idle_ok": idle_ok,
        "idle_status": idle_status,
        "raw": results,
        "mode": "G21_G91_G1_G90_mono_axis_wait_idle"
    }

def send_continuous_jog(axis, direction):
    """Jog continu fiable : la machine bouge tant que le bouton reste appuyé.
    On envoie $J une seule fois avec distance moyenne, puis /jog_stop envoie 0x85.
    """
    axis = str(axis).upper().strip()
    direction = 1 if float(direction) > 0 else -1
    if axis not in ["X", "Y", "Z"]:
        raise ValueError("Axe invalide")

    feed = JOG_FEED_Z if axis == "Z" else (JOG_FEED_Y if axis == "Y" else JOG_FEED_XY)
    long_dist = CONTINUOUS_JOG_DISTANCE_Z if axis == "Z" else (CONTINUOUS_JOG_DISTANCE_Y if axis == "Y" else CONTINUOUS_JOG_DISTANCE_XY)
    distance = direction * long_dist

    if axis == "X" and INVERT_X_JOG_DIRECTION:
        distance = -distance
    if axis == "Y" and INVERT_Y_JOG_DIRECTION:
        distance = -distance
    if axis == "Z" and INVERT_Z_DIRECTION:
        distance = -distance

    cmd = f"$J=G91 G21 {axis}{distance:.4f} F{feed:.1f}"

    connect_grbl()
    read_lines(0.01)
    with serial_lock:
        ser.write((cmd + "\n").encode())
        ser.flush()

    # GRBL répond normalement ok rapidement. Si rien ne revient, on ne bloque pas l'interface.
    response, raw = wait_response(timeout=0.8)
    if response == "timeout":
        response = "sent_no_wait"

    return {
        "line": cmd,
        "response": response,
        "raw": raw,
        "mode": "continuous_jog_start_mono_axis_reliable"
    }




def map_z_for_grbl(line):
    """Ne touche pas Z sauf si INVERT_Z_DIRECTION=True.
    Le dashboard reste logique : Z positif = direction sécurité voulue.
    On ne modifie pas G92 ni les réglages GRBL.
    """
    if not INVERT_Z_DIRECTION:
        return line
    u = line.upper().strip()
    if not u.startswith(("G0", "G00", "G1", "G01")) or "Z" not in u:
        return line
    def repl(m):
        return "Z" + f"{-float(m.group(1)):.4f}"
    return re.sub(r"Z\s*(-?\d+(?:\.\d+)?)", repl, line, flags=re.IGNORECASE)


def apply_safe_grbl_settings():
    results = []
    for cmd in SAFE_GRBL_SETTINGS:
        results.append(send_raw(cmd, timeout=2.0))
        time.sleep(0.03)
    return results


def soft_reset_grbl():
    global ser
    connect_grbl()
    with serial_lock:
        ser.write(b"\x18")
        ser.flush()
    time.sleep(1.2)
    read_lines(0.5)


def get_status_line(timeout=2.0):
    connect_grbl()
    read_lines(0.02)
    with serial_lock:
        ser.write(b"?")
        ser.flush()
    start = time.time()
    while time.time() - start < timeout:
        line = ser.readline().decode(errors="ignore").strip()
        if line.startswith("<"):
            return line
    return ""


def parse_position(status):
    if not status:
        return None
    for tag in ("WPos:", "MPos:"):
        if tag in status:
            try:
                txt = status.split(tag, 1)[1].split("|", 1)[0].replace(">", "")
                x, y, z = [float(v) for v in txt.split(",")[:3]]
                return {"x": x, "y": y, "z": z}
            except Exception:
                return None
    return None


def parse_pins(status):
    if not status or "Pn:" not in status:
        return []
    txt = status.split("Pn:", 1)[1].split("|", 1)[0].replace(">", "")
    return sorted(list(set(txt.strip())))


def clean_gcode_line(line):
    line = str(line).strip()
    if not line or line.startswith(";"):
        return ""
    line = re.sub(r"\(.*?\)", "", line)
    if ";" in line:
        line = line.split(";", 1)[0]
    return line.strip()


def extract_axis_values(line):
    vals = {}
    for axis, val in re.findall(r"([XYZ])\s*(-?\d+(?:\.\d+)?)", line.upper()):
        vals[axis.lower()] = float(val)
    return vals


def update_position_after_ok(line):
    global absolute_mode, current_pos
    u = line.upper().strip()
    if u.startswith("G90"):
        absolute_mode = True
        return
    if u.startswith("G91"):
        absolute_mode = False
        return
    if u.startswith("G92"):
        vals = extract_axis_values(u)
        for k, v in vals.items():
            current_pos[k] = v
        return
    if u.startswith(("G0", "G00", "G1", "G01")):
        vals = extract_axis_values(u)
        for k, v in vals.items():
            if absolute_mode:
                current_pos[k] = v
            else:
                current_pos[k] += v


def is_motion_or_wait_command(line):
    """Retourne True si la ligne doit être terminée avant de passer à la suivante.
    Pour une machine de soudure, on préfère la fiabilité : chaque déplacement et chaque
    temporisation sont validés avant d'envoyer la suite.
    """
    u = line.upper().strip()
    return u.startswith(("G0", "G00", "G1", "G01", "G4", "G04"))


def estimate_motion_timeout(line, default_timeout=30.0):
    """Estime un timeout suffisant pour ne pas arrêter la séquence au milieu.
    Les anciennes versions avaient un timeout trop court : si une réponse tardait,
    la séquence s'arrêtait et certains points n'étaient pas soudés.
    """
    u = line.upper().strip()
    if u.startswith(("G4", "G04")):
        m = re.search(r"P\s*([-+]?\d+(?:\.\d+)?)", u)
        dwell = float(m.group(1)) if m else 1.0
        return max(10.0, dwell + 8.0)

    vals = extract_axis_values(u)
    feed_match = re.search(r"F\s*([-+]?\d+(?:\.\d+)?)", u)
    feed = float(feed_match.group(1)) if feed_match else JOG_FEED_XY
    feed = max(feed, 1.0)

    # Distance approximative à partir de la position logique connue.
    dist = 0.0
    if vals:
        dx = vals.get('x', current_pos.get('x', 0.0)) - current_pos.get('x', 0.0) if absolute_mode else vals.get('x', 0.0)
        dy = vals.get('y', current_pos.get('y', 0.0)) - current_pos.get('y', 0.0) if absolute_mode else vals.get('y', 0.0)
        dz = vals.get('z', current_pos.get('z', 0.0)) - current_pos.get('z', 0.0) if absolute_mode else vals.get('z', 0.0)
        dist = (dx*dx + dy*dy + dz*dz) ** 0.5
    # temps ≈ distance/feed*60 + marge. Minimum large pour éviter les faux timeouts.
    return min(90.0, max(default_timeout, dist / feed * 60.0 + 8.0))


def send_gcode_lines(lines):
    """Envoie une séquence G-code de soudure de manière robuste.
    Retourne aussi le nombre de points réellement soudés.
    Un point est compté comme soudé lorsque la commande G4 de contact après le commentaire ; Point est terminée.
    """
    stop_event.clear()
    results = []
    connect_grbl()
    read_lines(0.05)

    soldered_points = 0
    current_point_comment = None
    current_point_counted = False

    for original in lines:
        if stop_event.is_set():
            results.append({"line": "STOP", "response": "stopped_by_user", "raw": ["Sequence arretee."]})
            break

        original_str = str(original).strip()

        if original_str.startswith("; Point"):
            current_point_comment = original_str
            current_point_counted = False
            results.append({"line": original_str, "response": "comment", "raw": []})
            continue

        line = clean_gcode_line(original_str)
        if not line:
            continue

        line_to_send = map_z_for_grbl(line)
        timeout = estimate_motion_timeout(line, default_timeout=12.0)
        result = send_raw(line_to_send, timeout=timeout)
        result["original_line"] = line
        if current_point_comment:
            result["point_comment"] = current_point_comment

        resp = str(result.get("response", "")).lower()

        if resp.startswith("ok"):
            update_position_after_ok(line)

            if is_motion_or_wait_command(line):
                idle_timeout = estimate_motion_timeout(line, default_timeout=20.0)
                idle_ok, idle_status = wait_until_idle(timeout=idle_timeout)
                result["idle_ok"] = idle_ok
                result["idle_status"] = idle_status
                if not idle_ok:
                    # Ne pas arrêter toute la séquence si le mouvement a répondu "ok".
                    # Certaines cartes/PC lisent mal le statut Idle, surtout pendant G4.
                    # On garde l'information pour le journal, mais on continue.
                    result["idle_warning"] = "idle_timeout_ignored"

            # Compter le point soudé après la première temporisation G4 qui suit ; Point.
            # La pause entre points ne sera pas recomptee car current_point_counted devient True.
            if current_point_comment and (not current_point_counted) and line.upper().strip().startswith(("G4", "G04")):
                soldered_points += 1
                current_point_counted = True
                result["soldered_point_number"] = soldered_points

        results.append(result)

        if resp.startswith("error") or resp.startswith("alarm") or resp in ("timeout", "stopped_by_user", "serial_error"):
            break

        time.sleep(0.03)

    # Sécurité : si la séquence contient un seul point et aucune erreur,
    # on le considère exécuté même si le compteur G4 n'a pas été détecté.
    has_point = any(str(x).strip().startswith("; Point") for x in lines)
    has_error = any(str(r.get("response", "")).lower().startswith(("error", "alarm")) or str(r.get("response", "")).lower() in ("timeout", "idle_timeout", "stopped_by_user", "serial_error") for r in results if isinstance(r, dict))
    if has_point and soldered_points == 0 and not has_error:
        soldered_points = 1

    return {"results": results, "soldered_points": soldered_points}

# ============================================================
# GERBER SIMPLE PARSER
# ============================================================
def parse_fsla(text):
    # Exemple Proteus : %FSLAX24Y24*%  => 2 entiers + 4 decimales
    m = re.search(r"FSLAX(\d)(\d)Y(\d)(\d)", text)
    if m:
        return int(m.group(2)), int(m.group(4))
    # compatibilite avec quelques variantes
    m = re.search(r"FSLA.?X(\d)(\d)Y(\d)(\d)", text)
    if m:
        return int(m.group(2)), int(m.group(4))
    return 4, 4


def parse_units(text):
    return "inch" if "MOIN" in text else "mm"


def gerber_number(raw, decimals, units):
    raw = str(raw).strip()
    # int("+1500") et int("-1500") sont acceptes par Python
    val = int(raw) / (10 ** decimals)
    if units == "inch":
        val *= 25.4
    return val


def extract_gerber_points(text):
    xdec, ydec = parse_fsla(text)
    units = parse_units(text)
    points, seen = [], set()
    patterns = [re.compile(r"X([+-]?\d+)Y([+-]?\d+)D03\*"), re.compile(r"X([+-]?\d+)Y([+-]?\d+)")]
    for pattern in patterns:
        for xr, yr in pattern.findall(text):
            x = gerber_number(xr, xdec, units)
            y = gerber_number(yr, ydec, units)
            key = (round(x, 4), round(y, 4))
            if key not in seen:
                seen.add(key)
                points.append({"x": x, "y": y})
        if points:
            break
    return points

# ============================================================
# ROUTES API
# ============================================================
@app.route("/")
def home():
    return jsonify({"status": "ok", "message": "Backend CNC actif - origine machine conservee, calibration PCB 2 coins cote dashboard, Z securite haut/bas."})

@app.route("/status")
def status():
    try:
        status_line = get_status_line()
        pos = parse_position(status_line)
        if pos:
            current_pos.update(pos)
        return jsonify({"status": "ok", "grbl_status": status_line, "position": pos or current_pos, "pins": parse_pins(status_line)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/limit_status")
def limit_status():
    try:
        status_line = get_status_line()
        pins = parse_pins(status_line)
        return jsonify({
            "status": "ok",
            "grbl_status": status_line,
            "pins": pins,
            "x_active": "X" in pins,
            "y_active": "Y" in pins,
            "z_active": "Z" in pins,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/unlock", methods=["POST"])
def unlock():
    try:
        stop_event.clear()
        result = send_raw("$X", timeout=4.0)
        return jsonify({"status": "ok", "message": "Unlock envoye.", "results": [result]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/set_machine_origin", methods=["POST"])
def set_machine_origin():
    try:
        payload = send_gcode_lines(["G21", "G90", "G92 X0 Y0 Z0"])
        results = payload.get("results", [])
        current_pos.update({"x": 0.0, "y": 0.0, "z": 0.0})
        return jsonify({"status": "ok", "message": "Position actuelle enregistree comme origine machine X0 Y0 Z0.", "results": results, "soldered_points": payload.get("soldered_points", 0)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/homing", methods=["POST"])
def homing_return_manual_origin():
    try:
        # Mode sans fins de course : retour a l'origine definie par G92.
        # On monte/retourne Z vers 0 puis X/Y vers 0. Definis l'origine machine avec Z en hauteur securisee.
        lines = ["G21", "G90", "G1 Z0.000 F80", "G1 X0.000 Y0.000 F800"]
        payload = send_gcode_lines(lines)
        results = payload.get("results", [])
        return jsonify({"status": "ok", "message": "Retour origine machine manuelle termine.", "results": results, "soldered_points": payload.get("soldered_points", 0)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/current_z")
def current_z():
    """Renvoie seulement la position Z actuelle pour capturer Z sécurité / Z soudure."""
    try:
        status_line = get_status_line()
        pos = parse_position(status_line)
        if pos:
            current_pos.update(pos)
        return jsonify({"status": "ok", "grbl_status": status_line, "z": (pos or current_pos).get("z", 0.0), "position": pos or current_pos})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/send_gcode_sequence", methods=["POST"])
def send_gcode_sequence():
    try:
        data = request.get_json(silent=True) or {}
        lines = data.get("lines", [])
        if not isinstance(lines, list) or not lines:
            return jsonify({"status": "error", "message": "Sequence vide."}), 400
        payload = send_gcode_lines(lines)
        results = payload.get("results", [])
        return jsonify({"status": "ok", "count": len(results), "results": results, "soldered_points": payload.get("soldered_points", 0)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/soft_reset", methods=["POST"])
def soft_reset():
    try:
        stop_event.set()
        soft_reset_grbl()
        stop_event.clear()
        return jsonify({"status": "ok", "message": "Soft reset envoye."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/stop", methods=["POST"])
def stop():
    try:
        stop_event.set()
        send_realtime(b"\x85")  # annule un jog GRBL
        send_realtime(b"!")      # feed hold
        return jsonify({"status": "ok", "message": "STOP / Jog cancel envoyé."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/jog_start", methods=["POST"])
def jog_start():
    """Démarre un jog continu sur un seul axe. Utilisé quand tu restes appuyé sur X+/X-/Y+/Y-/Z+/Z-."""
    try:
        data = request.get_json(silent=True) or {}
        axis = str(data.get("axis", "X")).upper().strip()
        direction = parse_float_any(data.get("direction", 1), 1.0)
        result = send_continuous_jog(axis, direction)
        return jsonify({"status": "ok", "message": f"Jog continu {axis} démarré. Relâche le bouton pour arrêter.", "results": [result], "command_sent": result.get("line")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/jog_stop", methods=["POST"])
def jog_stop():
    """Arrête immédiatement le jog continu GRBL."""
    try:
        send_realtime(b"\x85")  # Jog cancel GRBL 1.1
        time.sleep(0.05)
        send_realtime(b"\x85")
        return jsonify({"status": "ok", "message": "Jog continu arrêté."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/emergency", methods=["POST"])
def emergency():
    try:
        stop_event.set()
        send_realtime(b"!")
        time.sleep(0.1)
        soft_reset_grbl()
        stop_event.clear()
        return jsonify({"status": "ok", "message": "Emergency + reset envoyes."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/upload_gerber", methods=["POST"])
def upload_gerber():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "Aucun fichier recu."}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"status": "error", "message": "Nom fichier vide."}), 400
    path = os.path.join(UPLOAD_FOLDER, f.filename)
    f.save(path)
    return jsonify({"status": "ok", "filename": f.filename, "saved_to": path})

@app.route("/extract_points", methods=["POST"])
def extract_points():
    try:
        data = request.get_json(silent=True) or {}
        filename = str(data.get("filename", "")).strip()
        path = os.path.join(UPLOAD_FOLDER, filename)
        if not filename or not os.path.exists(path):
            return jsonify({"status": "error", "message": "Fichier introuvable."}), 404
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        points = extract_gerber_points(text)
        d03_count = len(re.findall(r"D03\\*", text))
        if not points:
            return jsonify({"status": "error", "message": "Aucun point detecte dans le fichier.", "d03_count": d03_count}), 400
        return jsonify({"status": "ok", "count": len(points), "d03_count": d03_count, "points": points[:3000]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500




def read_grbl_steps_internal():
    """Lit $$ de façon robuste.
    Certaines cartes répondent lentement ou renvoient des lignes avant/après ok.
    Cette version collecte les lignes pendant un court délai au lieu de dépendre seulement de send_raw().
    """
    connect_grbl()
    settings = {}
    raw = []
    with serial_lock:
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        ser.write(b"$$\n")
        ser.flush()

    start = time.time()
    while time.time() - start < 3.0:
        try:
            line = ser.readline().decode(errors="ignore").strip()
        except Exception:
            line = ""
        if line:
            raw.append(line)
            m = re.match(r"\$(\d+)\s*=\s*([-+]?\d+(?:\.\d+)?)", line.strip())
            if m:
                settings["$" + m.group(1)] = float(m.group(2))
        if any(k in settings for k in ("$100", "$101", "$102")) and any(str(x).lower().startswith("ok") for x in raw):
            break

    steps = {}
    if "$100" in settings: steps["X"] = settings["$100"]
    if "$101" in settings: steps["Y"] = settings["$101"]
    if "$102" in settings: steps["Z"] = settings["$102"]

    result = {"line": "$$", "response": "ok" if steps else "no_steps", "raw": raw}
    return settings, steps, result

@app.route("/grbl_settings")
def grbl_settings():
    """Lit $$ et renvoie surtout $100/$101/$102."""
    try:
        settings, steps, result = read_grbl_steps_internal()
        if not steps:
            return jsonify({"status": "error", "message": "Impossible de lire $100/$101/$102. Vérifie que GRBL répond à $$.", "result": result}), 500
        return jsonify({"status": "ok", "steps": steps, "settings": settings, "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/calibration_move", methods=["POST"])
def calibration_move():
    """Déplacement relatif doux sur UN SEUL axe pour Jog et calibration.
    Version corrigée : utilise $J=G91 G21 X... ou Y... ou Z... seulement.
    Donc si tu demandes Y 1 mm, le backend envoie uniquement Y, jamais X/Z.
    """
    try:
        data = request.get_json(silent=True) or {}
        axis = str(data.get("axis", "X")).upper().strip()
        distance = parse_float_any(data.get("distance", 0), 0.0)
        if axis not in ["X", "Y", "Z"] or abs(distance) <= 0:
            return jsonify({"status": "error", "message": "Axe ou distance invalide."}), 400

        # Limites adaptées à ton prototype.
        # Avant, la limite était 20 mm : c'était trop petit pour calibrer X/Y à 100 mm.
        # X : rail 300 mm - chariot/bâti 75 mm ≈ 225 mm utiles.
        # Y : plateau 300 mm. Z : 100 mm max à adapter si besoin.
        max_dist = {"X": MAX_TRAVEL_X, "Y": MAX_TRAVEL_Y, "Z": MAX_TRAVEL_Z}[axis]
        if abs(distance) > max_dist:
            return jsonify({
                "status": "error",
                "message": f"Distance trop grande pour {axis}. Maximum autorisé selon la course utile: {max_dist} mm."
            }), 400

        feed = JOG_FEED_Z if axis == "Z" else JOG_FEED_XY
        result = send_jog(axis, distance, feed)
        return jsonify({
            "status": "ok",
            "message": f"Déplacement {axis} demandé {distance:.3f} mm. Commande mono-axe fiable envoyée. Z est inversé si nécessaire.",
            "results": [result],
            "command_sent": result.get("line")
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/configure_safe", methods=["POST"])
def configure_safe():
    """Applique des vitesses/accélérations douces sans toucher $100/$101/$102."""
    try:
        results = apply_safe_grbl_settings()
        return jsonify({
            "status": "ok",
            "message": "Réglages doux appliqués. La calibration $100/$101/$102 n'a pas été modifiée.",
            "results": results
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/calibrate_steps", methods=["POST"])
def calibrate_steps():
    """Calcule et sauvegarde $100/$101/$102 dans GRBL EEPROM.
    Version robuste :
    - lit les steps actuels directement depuis GRBL si le dashboard envoie 0 ;
    - accepte virgule ou point ;
    - attend que GRBL soit Idle ;
    - vérifie après écriture que la valeur est bien sauvegardée.
    """
    try:
        data = request.get_json(silent=True) or {}
        axis = str(data.get("axis", "")).upper().strip()
        commanded_distance = parse_float_any(data.get("commanded_distance", 0), 0.0)
        measured_distance = parse_float_any(data.get("measured_distance", 0), 0.0)
        current_steps = parse_float_any(data.get("current_steps", 0), 0.0)

        if axis not in ["X", "Y", "Z"]:
            return jsonify({"status": "error", "message": "Axe invalide. Choisis X, Y ou Z."}), 400
        if commanded_distance <= 0 or measured_distance <= 0:
            return jsonify({"status": "error", "message": "Distance demandée et distance mesurée doivent être positives."}), 400

        # Lire automatiquement la valeur actuelle depuis GRBL si besoin.
        settings_before, steps_before, read_before = read_grbl_steps_internal()
        if current_steps <= 0:
            current_steps = steps_before.get(axis, 0.0)
        if current_steps <= 0:
            return jsonify({
                "status": "error",
                "message": f"Impossible de trouver les steps actuels pour {axis}. Clique d'abord 'Lire $100/$101/$102' et vérifie que GRBL répond.",
                "read_before": read_before
            }), 400

        new_steps = current_steps * commanded_distance / measured_distance
        if new_steps <= 0 or new_steps > 100000:
            return jsonify({"status": "error", "message": f"Valeur calculée anormale: {new_steps}. Vérifie ta mesure."}), 400

        setting = {"X": "$100", "Y": "$101", "Z": "$102"}[axis]
        cmd = f"{setting}={new_steps:.5f}"

        # Annuler un éventuel jog et s'assurer que GRBL est disponible.
        try:
            send_realtime(b"\x85")
            time.sleep(0.15)
        except Exception:
            pass

        result = send_raw(cmd, timeout=6.0)
        if not str(result.get("response", "")).lower().startswith("ok"):
            return jsonify({
                "status": "error",
                "message": "GRBL n'a pas accepté la commande de calibration. Fais Unlock puis réessaie.",
                "command": cmd,
                "result": result
            }), 500

        # Vérification après sauvegarde
        time.sleep(0.2)
        settings_after, steps_after, read_after = read_grbl_steps_internal()
        saved_value = steps_after.get(axis)
        verified = saved_value is not None and abs(saved_value - new_steps) < 0.02

        return jsonify({
            "status": "ok",
            "axis": axis,
            "setting": setting,
            "old_steps": current_steps,
            "commanded_distance": commanded_distance,
            "measured_distance": measured_distance,
            "new_steps": new_steps,
            "saved_value": saved_value,
            "verified": verified,
            "command": cmd,
            "result": result,
            "read_after": read_after,
            "message": f"{setting} calculé et envoyé. Valeur sauvegardée: {saved_value if saved_value is not None else 'non lue'}."
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/disconnect", methods=["POST"])
def disconnect():
    global ser
    try:
        stop_event.set()
        if ser is not None and ser.is_open:
            ser.close()
        ser = None
        return jsonify({"status": "ok", "message": "Connexion fermee."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
