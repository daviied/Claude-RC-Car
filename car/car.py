"""RC car client for Raspberry Pi 4.

- Receives Throttle/Steering from the server over Socket.IO.
- Streams the camera to the browser over WebRTC (aiortc).
- Stops the car if no command arrives for FAILSAFE_S seconds.

Config: environment variables (see car/.env.example).
"""
import asyncio
import os
import time

import socketio
from aiortc import (RTCConfiguration, RTCIceServer, RTCPeerConnection,
                    RTCSessionDescription, VideoStreamTrack)
from aiortc.contrib.media import MediaRelay

SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:5000")
CAR_TOKEN = os.environ.get("CAR_TOKEN", "")
CAMERA = os.environ.get("CAMERA", "picam")          # "picam" or "usb"
USB_DEVICE = os.environ.get("USB_DEVICE", "/dev/video0")
WIDTH = int(os.environ.get("VIDEO_WIDTH", "640"))
HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "480"))
FPS = int(os.environ.get("VIDEO_FPS", "30"))
FAILSAFE_S = float(os.environ.get("FAILSAFE_S", "0.5"))

sio = socketio.AsyncClient(reconnection=True, reconnection_delay=2, reconnection_delay_max=10)
relay = MediaRelay()
pcs = {}
cam_track = None

Throttle = 0
Steering = 0
last_update = 0.0


# ---------- Camera ----------
class PiCamTrack(VideoStreamTrack):
    """Pi Camera Module (CSI) through Picamera2."""

    def __init__(self):
        super().__init__()
        from picamera2 import Picamera2
        self.cam = Picamera2()
        self.cam.configure(self.cam.create_video_configuration(
            main={"size": (WIDTH, HEIGHT), "format": "RGB888"},
            controls={"FrameRate": FPS}))
        self.cam.start()

    async def recv(self):
        from av import VideoFrame
        pts, time_base = await self.next_timestamp()
        arr = await asyncio.to_thread(self.cam.capture_array)
        frame = VideoFrame.from_ndarray(arr, format="bgr24")  # RGB888 is BGR in memory
        frame.pts, frame.time_base = pts, time_base
        return frame


def make_track():
    if CAMERA == "usb":
        from aiortc.contrib.media import MediaPlayer
        return MediaPlayer(USB_DEVICE, format="v4l2", options={
            "video_size": f"{WIDTH}x{HEIGHT}", "framerate": str(FPS)}).video
    return PiCamTrack()


def get_track():
    global cam_track
    if cam_track is None:
        cam_track = make_track()
    return relay.subscribe(cam_track)


# ---------- Motor output ----------
def apply_controls(throttle, steering):
    """TODO: drive your ESC / motor driver and steering servo here.
    throttle and steering are floats from -1.0 to 1.0
    (keyboard gives -1/0/1, a game controller gives analog values)."""
    pass


def stop():
    global Throttle, Steering
    Throttle = Steering = 0


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


@sio.on("Update")
async def Update(data):
    global Throttle, Steering, last_update
    Throttle, Steering = data[0], data[1]
    last_update = time.monotonic()


@sio.on("webrtc_offer")
async def on_offer(data):
    sid = data["sid"]
    if sid in pcs:
        await pcs.pop(sid).close()

    ice = await sio.call("get_ice", timeout=5)
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[
        RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
        for s in ice]))
    pcs[sid] = pc

    @pc.on("connectionstatechange")
    async def on_state():
        print("WebRTC", sid, pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            if pcs.get(sid) is pc:
                pcs.pop(sid)

    pc.addTrack(get_track())
    await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
    await pc.setLocalDescription(await pc.createAnswer())
    await sio.emit("webrtc_answer", {"sid": sid,
                                     "sdp": pc.localDescription.sdp,
                                     "type": pc.localDescription.type})


# ---------- Control loop ----------
async def control_loop():
    while True:
        if time.monotonic() - last_update > FAILSAFE_S:
            stop()
        apply_controls(Throttle, Steering)
        await asyncio.sleep(0.02)


async def main():
    if not CAR_TOKEN:
        raise SystemExit("CAR_TOKEN must be set")
    asyncio.create_task(control_loop())
    while True:
        try:
            await sio.connect(SERVER_URL, auth={"role": "car", "token": CAR_TOKEN})
            await sio.wait()
        except Exception as e:
            print("Connection error!", e)
            stop()
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
