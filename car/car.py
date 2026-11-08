"""Claude-RC-Car client for Raspberry Pi 4.

- Receives PWM pulse widths [throttle_us, steering_us] from the server.
- Streams the camera to the browser over WebRTC (aiortc).
- Stops the car if no command arrives for FAILSAFE_S seconds.

Config: environment variables (see car/.env.example).
"""
import asyncio
import glob
import os
import signal
import time

import socketio
from aiortc import (RTCConfiguration, RTCIceServer, RTCPeerConnection,
                    RTCSessionDescription, VideoStreamTrack)
from aiortc.contrib.media import MediaRelay

SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:5000")
CAR_TOKEN = os.environ.get("CAR_TOKEN", "")
CAMERA = os.environ.get("CAMERA", "auto")           # "auto", "picam" or "usb"
USB_DEVICE = os.environ.get("USB_DEVICE", "auto")   # "auto" = first USB webcam
WIDTH = int(os.environ.get("VIDEO_WIDTH", "640"))
HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "480"))
FPS = int(os.environ.get("VIDEO_FPS", "20"))
FAILSAFE_S = float(os.environ.get("FAILSAFE_S", "0.5"))
# Each viewer gets its own software encoder (about 1 CPU core each on a Pi 4).
# Keep this at 1 or the Pi can overload and stop responding.
MAX_VIEWERS = int(os.environ.get("MAX_VIEWERS", "1"))
CONNECT_TIMEOUT_S = 30

sio = socketio.AsyncClient(reconnection=True, reconnection_delay=2, reconnection_delay_max=10)
relay = MediaRelay()
pcs = {}
cam_track = None

last_update = 0.0


# ---------- Camera ----------
class PiCamTrack(VideoStreamTrack):
    """Pi Camera Module (CSI) through Picamera2."""

    def __init__(self):
        super().__init__()
        from picamera2 import Picamera2
        if not has_csi_camera():
            raise RuntimeError("No Pi camera found (check the ribbon cable)")
        self.cam = Picamera2()
        try:
            self.cam.configure(self.cam.create_video_configuration(
                main={"size": (WIDTH, HEIGHT), "format": "RGB888"},
                controls={"FrameRate": FPS}, buffer_count=2))
            self.cam.start()
        except Exception:
            self.cam.close()  # release the camera so the next try can open it
            raise

    async def recv(self):
        from av import VideoFrame
        pts, time_base = await self.next_timestamp()
        arr = await asyncio.to_thread(self.cam.capture_array)
        frame = VideoFrame.from_ndarray(arr, format="bgr24")  # RGB888 is BGR in memory
        frame.pts, frame.time_base = pts, time_base
        return frame


def has_csi_camera():
    """True if a ribbon-cable camera is found (libcamera also lists USB webcams)."""
    try:
        from picamera2 import Picamera2
        return any("usb" not in c.get("Id", "") for c in Picamera2.global_camera_info())
    except Exception:
        return False


def find_usb_camera():
    if USB_DEVICE != "auto":
        return USB_DEVICE
    found = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    if not found:
        raise RuntimeError("No USB webcam found")
    return found[0]


def make_track():
    use_usb = CAMERA == "usb" or (CAMERA == "auto" and not has_csi_camera())
    if use_usb:
        from aiortc.contrib.media import MediaPlayer
        dev = find_usb_camera()
        print(f"USB camera: {dev}")
        # MJPEG gives full frame rate on most webcams (raw YUYV is often 5 fps)
        player = MediaPlayer(dev, format="v4l2", options={
            "input_format": "mjpeg", "video_size": f"{WIDTH}x{HEIGHT}",
            "framerate": str(FPS)})
        if player.video is None:
            raise RuntimeError(f"No video from {dev}")
        return player.video
    print("Pi camera (CSI)")
    return PiCamTrack()


camera_retry_at = 0.0


def get_track():
    """Open the camera once and share it. After a failure, wait 30 s before retrying."""
    global cam_track, camera_retry_at
    if cam_track is None:
        if time.monotonic() < camera_retry_at:
            raise RuntimeError("camera failed recently, retry later")
        try:
            cam_track = make_track()
        except Exception:
            camera_retry_at = time.monotonic() + 30
            raise
    return relay.subscribe(cam_track)


# ---------- Motor output (hardware PWM, 50 Hz servo pulses) ----------
# The server does all the math (trim, gain, ranges, invert) and sends
# [throttle_us, steering_us]. The Pi only outputs those pulse widths.
def env_f(name, default):
    return float(os.environ.get(name, default))


THROTTLE_PIN = int(os.environ.get("THROTTLE_PIN", "12"))   # ESC signal
STEERING_PIN = int(os.environ.get("STEERING_PIN", "13"))   # steering servo signal
# Hard safety limits on the Pi, whatever the server sends
PWM_MIN_US = env_f("PWM_MIN_US", 1000)
PWM_MAX_US = env_f("PWM_MAX_US", 2000)
PWM_PERIOD_NS = 20_000_000                                  # 50 Hz

# GPIO -> hardware PWM channel on Pi 4
PWM_CHANNEL = {12: 0, 18: 0, 13: 1, 19: 1}

# Failsafe output (no command / no connection). The server updates these.
failsafe_us = [env_f("FAILSAFE_THROTTLE_US", 1500), env_f("FAILSAFE_STEERING_US", 1500)]
target_us = list(failsafe_us)


class PwmOut:
    """One hardware PWM channel through /sys/class/pwm."""

    def __init__(self, gpio):
        self.channel = PWM_CHANNEL[gpio]
        self.chip = self._find_chip()
        self.path = f"{self.chip}/pwm{self.channel}"
        if not os.path.isdir(self.path):
            self._write(f"{self.chip}/export", self.channel)
        self._write(f"{self.path}/period", PWM_PERIOD_NS)
        self.last_us = None

    @staticmethod
    def _find_chip():
        base = "/sys/class/pwm"
        chips = sorted(c for c in os.listdir(base) if c.startswith("pwmchip"))
        for c in chips:  # prefer the main PWM block (pwm@7e20c000)
            if "20c000" in os.path.realpath(f"{base}/{c}"):
                return f"{base}/{c}"
        if not chips:
            raise RuntimeError("no PWM chip: add the pwm-2chan overlay and reboot")
        return f"{base}/{chips[0]}"

    @staticmethod
    def _write(path, value):
        # After export, udev needs a moment to give the gpio group access
        for _ in range(50):
            try:
                with open(path, "w") as f:
                    f.write(str(value))
                return
            except (PermissionError, FileNotFoundError):
                time.sleep(0.02)
        with open(path, "w") as f:
            f.write(str(value))

    def set_us(self, us):
        us = int(max(PWM_MIN_US, min(PWM_MAX_US, us)))
        if us == self.last_us:
            return
        self._write(f"{self.path}/duty_cycle", us * 1000)
        if self.last_us is None:
            self._write(f"{self.path}/enable", 1)
        self.last_us = us

    def off(self):
        try:
            self._write(f"{self.path}/enable", 0)
        except OSError:
            pass


esc = servo = None
pwm_error_shown = False


def init_pwm():
    global esc, servo, pwm_error_shown
    try:
        esc = PwmOut(THROTTLE_PIN)
        servo = PwmOut(STEERING_PIN)
        esc.set_us(failsafe_us[0])   # neutral first, so the ESC arms
        servo.set_us(failsafe_us[1])
        print(f"PWM ready: throttle GPIO {THROTTLE_PIN}, steering GPIO {STEERING_PIN}")
    except Exception as e:
        esc = servo = None
        if not pwm_error_shown:
            print("PWM not available, motors disabled:", repr(e))
            pwm_error_shown = True


def apply_pwm(throttle_us, steering_us):
    if esc is None:
        return
    esc.set_us(throttle_us)
    servo.set_us(steering_us)


def shutdown_pwm():
    if esc is not None:
        esc.set_us(failsafe_us[0])
        servo.set_us(failsafe_us[1])
        time.sleep(0.1)
        esc.off()
        servo.off()


def stop():
    target_us[:] = failsafe_us


def parse_pair(data):
    """Validate [throttle_us, steering_us] from the server."""
    t, st = float(data[0]), float(data[1])
    if not (t == t and st == st):  # NaN
        raise ValueError("NaN")
    return [max(PWM_MIN_US, min(PWM_MAX_US, t)), max(PWM_MIN_US, min(PWM_MAX_US, st))]


# ---------- Socket.IO events ----------
@sio.event
async def connect():
    print("I'm connected!")
    stop()


@sio.event
async def connect_error(arg):
    print("The connection failed!", arg)
    stop()


@sio.event
async def disconnect():
    print("I'm disconnected!")
    stop()


@sio.on("PWM")
async def on_pwm(data):
    global last_update
    try:
        target_us[:] = parse_pair(data)
    except (TypeError, ValueError, IndexError):
        return
    last_update = time.monotonic()


@sio.on("pwm_failsafe")
async def on_failsafe(data):
    try:
        failsafe_us[:] = parse_pair(data)
        print("Failsafe PWM:", failsafe_us)
    except (TypeError, ValueError, IndexError):
        pass


@sio.on("webrtc_offer")
async def on_offer(data):
    sid = data["sid"]

    # Close the old connection from this browser, and the oldest others
    # if over MAX_VIEWERS, so encoders never stack up.
    if sid in pcs:
        await close_pc(sid)
    while len(pcs) >= MAX_VIEWERS:
        await close_pc(next(iter(pcs)))

    pc = None
    try:
        track = get_track()
        try:
            ice = await sio.call("get_ice", timeout=5)
        except Exception:
            print("Server sent no ICE config, using public STUN only")
            ice = [{"urls": "stun:stun.l.google.com:19302"}]
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[
            RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
            for s in ice]))
        pcs[sid] = pc

        @pc.on("connectionstatechange")
        async def on_state():
            print("WebRTC", sid, pc.connectionState)
            if pc.connectionState in ("failed", "closed") and pcs.get(sid) is pc:
                await close_pc(sid)

        pc.addTrack(track)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
        await pc.setLocalDescription(await pc.createAnswer())
        await sio.emit("webrtc_answer", {"sid": sid,
                                         "sdp": pc.localDescription.sdp,
                                         "type": pc.localDescription.type})
        asyncio.create_task(connect_watchdog(sid, pc))
    except Exception as e:
        print("Video offer failed:", repr(e))
        if pc is not None and pcs.get(sid) is pc:
            await close_pc(sid)


async def close_pc(sid):
    pc = pcs.pop(sid, None)
    if pc is not None:
        try:
            await pc.close()
        except Exception as e:
            print("Close failed:", repr(e))


async def connect_watchdog(sid, pc):
    """Close a connection that never gets to 'connected'."""
    await asyncio.sleep(CONNECT_TIMEOUT_S)
    if pcs.get(sid) is pc and pc.connectionState != "connected":
        print("WebRTC", sid, "connect timeout")
        await close_pc(sid)


# ---------- Control loop ----------
async def control_loop():
    global esc, servo
    while True:
        if time.monotonic() - last_update > FAILSAFE_S:
            stop()
        try:
            apply_pwm(*target_us)
        except OSError as e:
            # Keep the loop (and failsafe) alive; try to set up PWM again
            print("PWM write failed:", repr(e))
            esc = servo = None
            await asyncio.sleep(1)
            init_pwm()
        await asyncio.sleep(0.02)


async def main():
    if not CAR_TOKEN:
        raise SystemExit("CAR_TOKEN must be set")
    init_pwm()
    asyncio.create_task(control_loop())
    while True:
        try:
            await sio.connect(SERVER_URL, auth={"role": "car", "token": CAR_TOKEN})
            await sio.wait()
        except Exception as e:
            print("Connection error!", e)
            stop()
            await asyncio.sleep(5)


def on_sigterm(*_):
    raise KeyboardInterrupt  # systemd stop -> same clean exit as Ctrl+C


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        shutdown_pwm()
