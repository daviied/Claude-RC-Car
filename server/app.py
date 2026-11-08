import base64
import hashlib
import hmac
import json
import os
import threading
import time
from functools import wraps

from flask import Flask, redirect, render_template, request, send_from_directory, session, url_for
from flask_socketio import SocketIO, emit
from werkzeug.middleware.proxy_fix import ProxyFix

# ---- Config (all from environment, see .env.example) ----
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
CAR_TOKEN = os.environ.get("CAR_TOKEN", "")
TURN_SECRET = os.environ.get("TURN_SECRET", "")
TURN_HOST = os.environ.get("TURN_HOST", "")
STUN_URL = os.environ.get("STUN_URL", "stun:stun.l.google.com:19302")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

if not APP_PASSWORD or not CAR_TOKEN:
    raise SystemExit("APP_PASSWORD and CAR_TOKEN must be set")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30
socketio = SocketIO(app)

speed = 0      # keyboard / touch: -1, 0, 1
turn = 0
pad_speed = 0.0  # game controller: -1.0 .. 1.0
pad_turn = 0.0
car_sid = None
phonecam_sid = None  # phone browser that streams a USB webcam (/camera page)
failed_logins = {}  # ip -> (count, first_fail_time)
last_pwm = None


# ---- Video quality presets (used by the car camera and the phone camera) ----
VIDEO_PRESETS = {
    "saver":  {"name": "Data saver", "width": 320,  "height": 240,  "fps": 15, "bitrate": 250_000},
    "low":    {"name": "Low",        "width": 640,  "height": 480,  "fps": 20, "bitrate": 600_000},
    "medium": {"name": "Medium",     "width": 640,  "height": 480,  "fps": 30, "bitrate": 1_200_000},
    "high":   {"name": "High",       "width": 1280, "height": 720,  "fps": 30, "bitrate": 2_500_000},
    "max":    {"name": "Max",        "width": 1920, "height": 1080, "fps": 30, "bitrate": 4_000_000},
}


def preset_label(p):
    mb_per_hour = p["bitrate"] * 3600 / 8 / 1e6
    size = f"{mb_per_hour / 1000:.1f} GB" if mb_per_hour >= 1000 else f"{mb_per_hour:.0f} MB"
    return f'{p["name"]}: {p["height"]}p, {p["bitrate"] / 1e6:g} Mbps (~{size}/h)'


# ---- Tuning settings (server does all the math, the Pi only outputs PWM) ----
# key: (label, default, min, max, step). bool settings use min=max=None.
SETTINGS_SPEC = {
    "throttle_gain":         ("Speed (max power)",        0.5,  0.0, 1.0,  0.05),
    "throttle_neutral_us":   ("Throttle neutral (us)",   1500, 1000, 2000, 5),
    "throttle_fwd_range_us": ("Forward range (us)",       500,    0, 1000, 10),
    "throttle_rev_range_us": ("Reverse range (us)",       500,    0, 1000, 10),
    "throttle_invert":       ("Reverse throttle",       False, None, None, None),
    "steer_center_us":       ("Steering trim / center (us)", 1500, 1000, 2000, 5),
    "steer_range_us":        ("Steering range (us)",      400,    0, 1000, 10),
    "steer_gain":            ("Steering gain",            1.0,  0.0, 1.0,  0.05),
    "steer_invert":          ("Reverse steering",       False, None, None, None),
    "pwm_min_us":            ("PWM min limit (us)",      1000,  500, 1500, 10),
    "pwm_max_us":            ("PWM max limit (us)",      2000, 1500, 2500, 10),
    "video_from_phone":      ("Video from phone camera (/camera)", False, None, None, None),
    "video_quality":         ("Video quality",        "medium", None, None, None),
    "video_rotate":          ("Rotate video 180°",       False, None, None, None),
    "video_mirror":          ("Mirror video (left/right)", False, None, None, None),
}
settings = {k: v[1] for k, v in SETTINGS_SPEC.items()}
_save_timer = None


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            for k, v in json.load(f).items():
                if k in SETTINGS_SPEC:
                    settings[k] = validate_setting(k, v)
        print("Settings loaded from", SETTINGS_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        print("Settings load failed, using defaults:", repr(e))


def save_settings_now():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        print("Settings save failed (kept in memory):", repr(e))


def save_settings():
    """Save 1 s after the last change, so slider drags do not write every step."""
    global _save_timer
    if _save_timer:
        _save_timer.cancel()
    _save_timer = threading.Timer(1.0, save_settings_now)
    _save_timer.daemon = True
    _save_timer.start()


def validate_setting(key, value):
    label, default, lo, hi, step = SETTINGS_SPEC[key]
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, str):  # choice from VIDEO_PRESETS
        if value not in VIDEO_PRESETS:
            raise ValueError("unknown preset")
        return value
    v = float(value)
    if v != v:  # NaN
        raise ValueError("NaN")
    v = max(lo, min(hi, v))
    return int(round(v)) if isinstance(default, int) else round(v, 3)


def settings_payload():
    out = []
    for k, spec in SETTINGS_SPEC.items():
        item = {"key": k, "label": spec[0], "value": settings[k], "default": spec[1],
                "min": spec[2], "max": spec[3], "step": spec[4]}
        if k == "video_quality":
            item["choices"] = [[key, preset_label(p)] for key, p in VIDEO_PRESETS.items()]
        out.append(item)
    return out


def video_preset():
    return VIDEO_PRESETS[settings["video_quality"]]


def send_quality():
    for sid in (car_sid, phonecam_sid):
        if sid:
            socketio.emit("video_quality", video_preset(), to=sid)


def compute_pwm(t, s):
    """Throttle/steering -1..1 -> [throttle_us, steering_us]."""
    c = settings
    if c["throttle_invert"]:
        t = -t
    if c["steer_invert"]:
        s = -s
    t *= c["throttle_gain"]
    s *= c["steer_gain"]
    thr = c["throttle_neutral_us"] + t * (c["throttle_fwd_range_us"] if t > 0 else c["throttle_rev_range_us"])
    st = c["steer_center_us"] + s * c["steer_range_us"]
    lo, hi = c["pwm_min_us"], c["pwm_max_us"]
    return [int(round(max(lo, min(hi, thr)))), int(round(max(lo, min(hi, st))))]


def failsafe_pwm():
    return compute_pwm(0.0, 0.0)


load_settings()


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def ice_servers():
    """STUN + time-limited TURN credentials (coturn use-auth-secret)."""
    servers = [{"urls": STUN_URL}]
    if TURN_SECRET and TURN_HOST:
        username = f"{int(time.time()) + 3600}:rc"
        cred = base64.b64encode(
            hmac.new(TURN_SECRET.encode(), username.encode(), hashlib.sha1).digest()
        ).decode()
        servers.append({
            "urls": [f"turn:{TURN_HOST}:3478?transport=udp", f"turn:{TURN_HOST}:3478?transport=tcp"],
            "username": username,
            "credential": cred,
        })
    return servers


# ---- HTTP routes ----
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = request.remote_addr
        count, first = failed_logins.get(ip, (0, time.time()))
        if time.time() - first > 900:
            count, first = 0, time.time()
        if count >= 10:
            error = "Too many attempts. Try again later."
        elif hmac.compare_digest(request.form.get("password", ""), APP_PASSWORD):
            failed_logins.pop(ip, None)
            session.permanent = True
            session["auth"] = True
            return redirect(url_for("index"))
        else:
            failed_logins[ip] = (count + 1, first)
            error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/camera")
@login_required
def camera():
    return render_template("camera.html")


@app.route("/sw.js")
def service_worker():
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest",
                               mimetype="application/manifest+json")


# ---- Socket.IO ----
def clamp(v):
    return max(-1.0, min(1.0, v))


def send_update():
    # Keyboard and controller add together, clamp to -1..1, then map to PWM
    global last_pwm
    pwm = compute_pwm(clamp(speed + pad_speed), clamp(turn + pad_turn))
    if car_sid:
        socketio.emit("PWM", pwm, to=car_sid)
    if pwm != last_pwm:
        last_pwm = pwm
        socketio.emit("pwm_out", pwm)  # live display in the browser


def send_failsafe():
    if car_sid:
        socketio.emit("pwm_failsafe", failsafe_pwm(), to=car_sid)


def video_sid():
    """Socket of the active video source (car or phone camera)."""
    return phonecam_sid if settings["video_from_phone"] else car_sid


def video_status():
    return {"source": "phone" if settings["video_from_phone"] else "car",
            "online": video_sid() is not None}


def is_source(sid):
    return sid is not None and sid in (car_sid, phonecam_sid)


@socketio.on("connect")
def on_connect(auth):
    global car_sid, phonecam_sid
    if auth and auth.get("role") == "car":
        if not hmac.compare_digest(str(auth.get("token", "")), CAR_TOKEN):
            return False
        car_sid = request.sid
        print("Car connected")
        socketio.emit("car_status", True)
        socketio.emit("video_status", video_status())
        emit("pwm_failsafe", failsafe_pwm())
        emit("video_quality", video_preset())
        if settings["video_from_phone"]:
            emit("video_stop")
        return
    if not session.get("auth"):
        return False  # reject unauthenticated browsers
    if auth and auth.get("role") == "phonecam":
        if phonecam_sid:
            socketio.emit("replaced", to=phonecam_sid)  # only one phone camera
        phonecam_sid = request.sid
        print("Phone camera connected")
        emit("video_quality", video_preset())
        socketio.emit("video_status", video_status())
        return
    print("Browser connected")
    emit("car_status", car_sid is not None)
    emit("video_status", video_status())
    emit("settings", settings_payload())
    emit("pwm_out", last_pwm or failsafe_pwm())


@socketio.on("disconnect")
def on_disconnect(reason=None):
    global car_sid, phonecam_sid, speed, turn, pad_speed, pad_turn
    if request.sid == car_sid:
        car_sid = None
        print("Car disconnected:", reason)
        socketio.emit("car_status", False)
        socketio.emit("video_status", video_status())
    elif request.sid == phonecam_sid:
        phonecam_sid = None
        print("Phone camera disconnected:", reason)
        socketio.emit("video_status", video_status())
    else:
        # Video sources stop sending to this browser
        for sid in (car_sid, phonecam_sid):
            if sid:
                socketio.emit("viewer_left", request.sid, to=sid)
        # Driver left: stop the car
        speed = turn = 0
        pad_speed = pad_turn = 0.0
        send_update()
        print("Browser disconnected:", reason)


@socketio.on("my_ping")
def my_ping():
    return "pong"


@socketio.on("get_ice")
def get_ice():
    return ice_servers()


@socketio.on("webrtc_offer")
def webrtc_offer(data):
    if is_source(request.sid):
        return
    target = video_sid()
    if target is None:
        emit("webrtc_error", "phone camera offline" if settings["video_from_phone"] else "car offline")
        return
    socketio.emit("webrtc_offer",
                  {"sid": request.sid, "sdp": data["sdp"], "type": data["type"]}, to=target)


@socketio.on("webrtc_answer")
def webrtc_answer(data):
    # Only the active source may answer (a late answer from the other one is dropped)
    if request.sid != video_sid():
        return
    socketio.emit("webrtc_answer", {"sdp": data["sdp"], "type": data["type"]}, to=data["sid"])


@socketio.on("video_restart")
def video_restart():
    """The car reopened its camera (new quality): viewers reconnect."""
    if request.sid == car_sid and not settings["video_from_phone"]:
        socketio.emit("video_status", video_status())


def video_source_changed():
    """Stop the old source (saves hotspot data) and make viewers reconnect."""
    old = car_sid if settings["video_from_phone"] else phonecam_sid
    if old:
        socketio.emit("video_stop", to=old)
    socketio.emit("video_status", video_status())


@socketio.on("set_setting")
def set_setting(data):
    if is_source(request.sid):
        return
    old_source = settings["video_from_phone"]
    old_quality = settings["video_quality"]
    try:
        key = data["key"]
        if key not in SETTINGS_SPEC:
            return
        settings[key] = validate_setting(key, data["value"])
    except (TypeError, ValueError, KeyError):
        return
    if settings["pwm_min_us"] > settings["pwm_max_us"]:
        settings["pwm_min_us"], settings["pwm_max_us"] = settings["pwm_max_us"], settings["pwm_min_us"]
    save_settings()
    socketio.emit("settings", settings_payload())
    if settings["video_from_phone"] != old_source:
        video_source_changed()
    if settings["video_quality"] != old_quality:
        send_quality()
    send_failsafe()
    send_update()


@socketio.on("reset_settings")
def reset_settings():
    if is_source(request.sid):
        return
    old_source = settings["video_from_phone"]
    old_quality = settings["video_quality"]
    settings.update({k: v[1] for k, v in SETTINGS_SPEC.items()})
    save_settings()
    socketio.emit("settings", settings_payload())
    if settings["video_from_phone"] != old_source:
        video_source_changed()
    if settings["video_quality"] != old_quality:
        send_quality()
    send_failsafe()
    send_update()


@socketio.on("Pad")
def Pad(data):
    """Analog game controller input: [throttle, steering], each -1.0 .. 1.0."""
    global pad_speed, pad_turn
    if is_source(request.sid):
        return
    try:
        pad_speed, pad_turn = clamp(float(data[0])), clamp(float(data[1]))
    except (TypeError, ValueError, IndexError):
        return
    send_update()


@socketio.on("Key_Press")
def Key_Press(arg):
    global speed, turn, pad_speed, pad_turn
    if is_source(request.sid):
        return
    match arg:
        case "W_Down" | "S_Up":
            speed += 1
        case "W_Up" | "S_Down":
            speed -= 1
        case "A_Down" | "D_Up":
            turn -= 1
        case "A_Up" | "D_Down":
            turn += 1
        case "Stop":
            speed = turn = 0
            pad_speed = pad_turn = 0.0
        # Q/E/G/H: add your extra functions here
        # "Heartbeat": no change, just re-sends the current state

    speed = max(-1, min(1, speed))
    turn = max(-1, min(1, turn))
    send_update()


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
