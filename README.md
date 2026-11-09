# Claude-RC-Car

Drive an RC car from a browser. Controls go over Socket.IO. Camera video goes over WebRTC.

```
Browser ──Socket.IO (controls + signaling)──► Server (Docker) ◄──Socket.IO── Raspberry Pi 4
   ▲                                                                          │
   └───────────── WebRTC video (through TURN, because of LTE CGNAT) ◄─────────┘
```

## Folders

| Path | What |
|---|---|
| `server/` | Flask + Socket.IO web app (password login, PWA) |
| `car/` | Pi client: controls, camera, failsafe |
| `docker-compose.yml` | Server + coturn TURN relay, for CasaOS |

## Server setup

1. Push this repo to GitHub. The workflow builds `ghcr.io/daviied/claude-rc-car` (amd64 + arm64).
2. In GitHub → Packages → `claude-rc-car` → set visibility to public (or log in to ghcr on the host).
3. In CasaOS, import `docker-compose.yml`. Set every value from `.env.example`.
4. nginx reverse proxy → `http://HOST:5000`, with WebSocket support:
   ```nginx
   location / {
       proxy_pass http://HOST:5000;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_read_timeout 86400;
   }
   ```
5. Forward these ports on your router to the coturn host (TURN does not go through nginx):
   - `3478` UDP + TCP
   - `49160-49200` UDP

## Pi setup (Ubuntu Server 26.10 on Pi 4)

```bash
git clone https://github.com/daviied/Claude-RC-Car.git ~/Claude-RC-Car
cd ~/Claude-RC-Car/car
cp .env.example .env   # then edit SERVER_URL and CAR_TOKEN
sudo ./install.sh      # installs apt packages + boot service, starts it
journalctl -u claude-rc-car -f
```

The program starts at boot and restarts if it crashes. Write your motor/servo code in `apply_controls()` in `car/car.py`.

## Local test (no Docker)

```bash
pip install -r server/requirements.txt
APP_PASSWORD=test CAR_TOKEN=test COOKIE_SECURE=false python server/app.py
```

## Controls

| Input | Throttle | Steering | Actions |
|---|---|---|---|
| Keyboard | W/S or ↑/↓ | A/D or ←/→ | Q E G H |
| Touch | ▲ ▼ | ◀ ▶ | – |
| Game controller | RT forward, LT reverse | Left stick X | D-pad ◀▶ steering trim, ▲▼ throttle neutral trim · Y/A speed +/− · B/X steering gain +/− |

Controller: Xbox, PlayStation and most USB/Bluetooth pads. Press any button once after the page opens so the browser detects it. Values are analog (-1.0 to 1.0). When a controller connects, the camera fills the screen and the phone locks to landscape (Android; tap once if the browser asks). The screen stays awake while the page is open.

Camera: `CAMERA=auto` uses a Pi camera (ribbon cable) if one is found, else the first USB webcam.

Video quality: the dropdowns under the video set the quality for **car video on Wi-Fi**, **car video on hotspot** and the **phone camera** (Minimum 240p / 0.1 Mbps up to Max 1080p / 4 Mbps; each shows its data use per hour). The car uses the Wi-Fi or hotspot preset for the network it is on now ("in use"). The car caps resolution at `VIDEO_MAX_HEIGHT` (default 720), because the Pi 4 encodes video in software.

## Car network (Wi-Fi first, phone hotspot as backup)

The car uses campus Wi-Fi when it is strong enough, and the phone (Ethernet adapter or USB tethering) when it is not. This saves hotspot data.

- **Car network** dropdown: *Auto* (Wi-Fi first), *Wi-Fi only*, *Phone hotspot only*. If the chosen network stops working, the car uses the other one, so it stays online and you can switch back.
- Auto moves to Wi-Fi when it is strong for `WIFI_STABLE_S` (6 s): signal ≥ `WIFI_MIN_SIGNAL` (40 %), loss ≤ `WIFI_MAX_LOSS` (10 %), round trip ≤ `WIFI_MAX_RTT_MS` (250 ms). It leaves Wi-Fi when it is weak for `WIFI_POOR_S` (5 s) or stops working (about 4 s).
- Header pill: active network and quality (▂▄▆█ good, ▂▄▆ fair, ▂ poor). Tap it for each network's round trip, loss and signal.
- **Car network** panel: data used per network kind and per interface (counted on the Pi, kept across restarts, Reset button), and the Wi-Fi list: Scan, tap a network to connect (school Wi-Fi asks for username + password, PEAP/MSCHAPv2), Forget. Passwords go only to the Pi (NetworkManager); the server does not save them.
- Strongest Wi-Fi: while Wi-Fi is weak or not in use, the car checks every `WIFI_ROAM_S` (20 s) and connects to the strongest saved network (at least `WIFI_ROAM_GAIN` = 15 % stronger). Save all campus networks once in the Wi-Fi list.
- A network switch reconnects the car (about 1–3 s). The failsafe stops the car during the gap.
- If the Pi joins the phone hotspot over Wi-Fi, put the hotspot name in `HOTSPOT_SSIDS` so it counts as hotspot.
- Captive-portal Wi-Fi (web login page) does not work: the car sees it as "not working" and stays on the hotspot.

Setup (once, on the Pi, where you can reach it with a keyboard if the network drops):

```bash
cd ~/Claude-RC-Car/car
sudo bash setup-network.sh   # NetworkManager + permission for the car program
```

### Phone camera (saves hotspot data)

Video can come from a phone on the car instead of the Pi. The video then uses the phone's own data, not the hotspot.

1. Plug a USB webcam into the phone (USB-C / OTG). Android 14+ with Chrome works on most phones. iPhones do not support USB webcams in the browser.
2. On that phone, open `https://<your server>/camera`, log in, and allow camera access. Pick the camera and quality. Keep the page open (the screen stays on).
3. On the drive page, open Tuning and turn on **Video from phone camera**. The car stops sending video. Turn it off to use the car camera again.

## Tuning (trim, speed, ...)

Open **Tuning** under the video on the website. The server does all the math and sends the Pi only the PWM pulse widths, so changes work at once with no restart on the Pi. Settings are saved in the `claude-rc-car-data` Docker volume.

| Setting | What it does |
|---|---|
| Speed (max power) | Throttle gain, 0–1. Start at 0.5 |
| Throttle neutral | ESC stop pulse (1500 µs). Change it if the motor creeps |
| Forward / reverse range | µs from neutral at full throttle |
| Steering trim / center | Servo center. `[` and `]` keys change it in 5 µs steps |
| Steering range / gain | How far the wheels turn |
| Reverse throttle / steering | Change direction |
| PWM min / max limit | Server-side clamp. The Pi also clamps to `PWM_MIN_US`/`PWM_MAX_US` in `car/.env` |

## Safety

- Car stops if no command arrives for 0.5 s (`FAILSAFE_S`). The browser sends a heartbeat every 200 ms.
- Car stops if the driver's browser disconnects, loses focus, or is hidden.

## Notes

- aiortc encodes video in software. 640×480 @ 30 fps is fine on a Pi 4. For more, use hardware H.264 (MediaMTX / go2rtc).
- PWA icon is SVG. For best iOS home-screen support, add 192 px and 512 px PNG icons.
