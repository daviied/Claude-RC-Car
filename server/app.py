import base64
import hashlib
import hmac
import os
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
failed_logins = {}  # ip -> (count, first_fail_time)


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
    # Keyboard and controller add together, then clamp to -1..1
    if car_sid:
        socketio.emit("Update", [round(clamp(speed + pad_speed), 3),
                                 round(clamp(turn + pad_turn), 3)], to=car_sid)


@socketio.on("connect")
def on_connect(auth):
    global car_sid
    if auth and auth.get("role") == "car":
        if not hmac.compare_digest(str(auth.get("token", "")), CAR_TOKEN):
            return False
        car_sid = request.sid
        print("Car connected")
        socketio.emit("car_status", True)
        return
    if not session.get("auth"):
        return False  # reject unauthenticated browsers
    print("Browser connected")
    emit("car_status", car_sid is not None)


@socketio.on("disconnect")
def on_disconnect(reason=None):
    global car_sid, speed, turn, pad_speed, pad_turn
    if request.sid == car_sid:
        car_sid = None
        print("Car disconnected:", reason)
        socketio.emit("car_status", False)
    else:
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
    if request.sid == car_sid:
        return
    if car_sid is None:
        emit("webrtc_error", "Car not connected")
        return
    socketio.emit("webrtc_offer",
                  {"sid": request.sid, "sdp": data["sdp"], "type": data["type"]}, to=car_sid)


@socketio.on("webrtc_answer")
def webrtc_answer(data):
    if request.sid != car_sid:
        return
    socketio.emit("webrtc_answer", {"sdp": data["sdp"], "type": data["type"]}, to=data["sid"])


@socketio.on("Pad")
def Pad(data):
    """Analog game controller input: [throttle, steering], each -1.0 .. 1.0."""
    global pad_speed, pad_turn
    if request.sid == car_sid:
        return
    try:
        pad_speed, pad_turn = clamp(float(data[0])), clamp(float(data[1]))
    except (TypeError, ValueError, IndexError):
        return
    send_update()


@socketio.on("Key_Press")
def Key_Press(arg):
    global speed, turn, pad_speed, pad_turn
    if request.sid == car_sid:
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
