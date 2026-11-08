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
| Game controller | RT forward, LT reverse (or left stick Y) | Left stick X | A B X Y = Q E G H |

Controller: Xbox, PlayStation and most USB/Bluetooth pads. Press any button once after the page opens so the browser detects it. Values are analog (-1.0 to 1.0).

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
