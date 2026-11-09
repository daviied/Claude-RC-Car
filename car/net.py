"""Car network: use Wi-Fi when it works, else the phone (Ethernet / USB tethering).

- Tests each network every PROBE_S seconds: a TCP connect to the server,
  sent out through that one interface.
- Auto mode: Wi-Fi first (saves hotspot data). Wi-Fi must work for
  WIFI_STABLE_S seconds before the car moves to it, so spotty Wi-Fi
  does not make the car switch all the time.
- Switches by changing route metrics in NetworkManager (lowest metric wins).
- Scans, connects and forgets Wi-Fi networks for the web UI (nmcli).

Needs NetworkManager and permission for the service user: sudo bash setup-network.sh
"""
import asyncio
import collections
import json
import os
import shutil
import socket
import time
import urllib.parse

WIFI_IFACE = os.environ.get("WIFI_IFACE", "wlan0")
# Wi-Fi networks that are a phone hotspot (they count as hotspot, not Wi-Fi)
HOTSPOT_SSIDS = {s.strip() for s in os.environ.get("HOTSPOT_SSIDS", "").split(",") if s.strip()}
PROBE_S = float(os.environ.get("NET_PROBE_S", "2"))
PROBE_TIMEOUT_S = 2.0
FAILS_TO_DROP = 2                                  # failed tests in a row = link down
WIFI_STABLE_S = float(os.environ.get("WIFI_STABLE_S", "6"))
# "Strong enough" Wi-Fi: the car moves to it only above these limits
WIFI_MIN_SIGNAL = int(os.environ.get("WIFI_MIN_SIGNAL", "40"))      # %
WIFI_MAX_LOSS = int(os.environ.get("WIFI_MAX_LOSS", "10"))          # % of recent tests
WIFI_MAX_RTT_MS = float(os.environ.get("WIFI_MAX_RTT_MS", "250"))
# Leave Wi-Fi when it stays poor this long (and the hotspot works)
WIFI_POOR_S = float(os.environ.get("WIFI_POOR_S", "5"))
# Change to the strongest saved Wi-Fi: check every WIFI_ROAM_S seconds while
# Wi-Fi is weak or not in use; move only if WIFI_ROAM_GAIN % stronger. 0 = off
WIFI_ROAM_S = float(os.environ.get("WIFI_ROAM_S", "20"))
WIFI_ROAM_GAIN = int(os.environ.get("WIFI_ROAM_GAIN", "15"))
ACTIVE_METRIC, IDLE_METRIC = 50, 700
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
TRAFFIC_FILE = os.environ.get("TRAFFIC_FILE", "traffic.json")
HAS_NM = shutil.which("nmcli") is not None
NO_NM = "NetworkManager is not set up on the car. Run: sudo bash setup-network.sh"


async def run(*args, timeout=40):
    """Run a command. Returns (exit code, stdout, stderr)."""
    try:
        p = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace"), err.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        p.kill()
        return 1, "", "timed out"
    except OSError as e:
        return 1, "", str(e)


def nm_fields(line):
    """Split one line of 'nmcli -t' output (':' separates, '\\' escapes)."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return out


def nm_error(err):
    return err.replace("Error: ", "").splitlines()[0] if err else "failed"


class Link:
    def __init__(self, iface):
        self.iface = iface
        self.up = False
        self.ok_since = None
        self.fails = FAILS_TO_DROP
        self.rtt = None
        self.hist = collections.deque(maxlen=15)  # last test results (True = ok)
        self.weak_since = None    # when the link became too weak
        self.strong_since = None  # when the link became strong

    def loss(self):
        return round(100 * self.hist.count(False) / len(self.hist)) if self.hist else None

    def quality(self, signal=None):
        """good / fair / poor from packet loss, round trip and Wi-Fi signal."""
        if not self.usable():
            return "down"
        loss, rtt = self.loss() or 0, self.rtt or 0
        if loss > 20 or rtt > 400 or (signal is not None and signal < 30):
            return "poor"
        if loss > 5 or rtt > 150 or (signal is not None and signal < 50):
            return "fair"
        return "good"

    def usable(self):
        return self.fails < FAILS_TO_DROP

    def stable_for(self):
        return time.monotonic() - self.ok_since if self.ok_since and self.usable() else 0


class Meter:
    """Total bytes per interface and per network kind, kept across restarts.
    Linux counters reset when an interface goes down (phone unplugged): then
    the new count is added as it is."""

    def __init__(self):
        self.data = self.empty()
        self.last = {}
        self.saved_at = 0.0
        try:
            with open(TRAFFIC_FILE) as f:
                d = json.load(f)
            if isinstance(d.get("ifaces"), dict) and isinstance(d.get("kinds"), dict):
                self.data = d
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def empty():
        return {"since": time.strftime("%Y-%m-%d %H:%M"), "ifaces": {}, "kinds": {}}

    @staticmethod
    def read(iface):
        b = f"/sys/class/net/{iface}/statistics/"
        with open(b + "rx_bytes") as rx, open(b + "tx_bytes") as tx:
            return int(rx.read()), int(tx.read())

    def update(self, kind_of):
        try:
            ifaces = [i for i in os.listdir("/sys/class/net") if i != "lo"]
        except OSError:
            return
        for i in ifaces:
            try:
                cur = self.read(i)
            except (OSError, ValueError):
                continue
            prev = self.last.get(i)
            self.last[i] = cur
            if prev is None:
                continue  # first reading after start: only the baseline
            d = [c - p if c >= p else c for c, p in zip(cur, prev)]
            if not any(d):
                continue
            for table, key in (("ifaces", i), ("kinds", kind_of(i))):
                t = self.data[table].setdefault(key, {"rx": 0, "tx": 0})
                t["rx"] += d[0]
                t["tx"] += d[1]
        if time.monotonic() - self.saved_at > 60:
            self.save()

    def save(self):
        self.saved_at = time.monotonic()
        try:
            tmp = TRAFFIC_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, TRAFFIC_FILE)
        except OSError as e:
            print("Traffic save failed:", e)

    def reset(self):
        self.data = self.empty()
        self.save()


class NetMonitor:
    def __init__(self, server_url, on_switch, on_report):
        u = urllib.parse.urlparse(server_url)
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.addr = None
        self.on_switch = on_switch     # async (old_iface, new_iface)
        self.on_report = on_report     # async (dict)
        self.mode = "auto"
        self.links = {}
        self.active = None
        self.note = ""
        self.wifi_ssid = ""
        self.wifi_signal = None
        self.last_report = None
        self.last_report_t = 0.0
        self.scan_cache = {}
        self.warned = set()
        self.meter = Meter()
        self.roam_at = time.monotonic() + 10
        self.roaming = False

    # ---------- Status ----------
    def kind(self, iface):
        if iface == WIFI_IFACE and self.wifi_ssid not in HOTSPOT_SSIDS:
            return "wifi"
        if iface == WIFI_IFACE or iface in self.links or iface.startswith(("eth", "en", "usb")):
            return "hotspot"
        return "other"  # docker, VPN, ...

    def signal(self, iface):
        return self.wifi_signal if iface == WIFI_IFACE else None

    def report(self):
        a = self.links.get(self.active)
        return {
            "active": self.active, "kind": self.kind(self.active) if self.active else None,
            "ssid": self.wifi_ssid if self.active == WIFI_IFACE else "",
            "wifi_ssid": self.wifi_ssid, "rtt": a.rtt if a else None,
            "loss": a.loss() if a else None, "signal": self.signal(self.active),
            "quality": a.quality(self.signal(self.active)) if a else "down",
            "mode": self.mode, "note": self.note, "traffic": self.meter.data,
            "links": [{"iface": l.iface, "kind": self.kind(l.iface), "up": l.usable(),
                       "rtt": l.rtt, "loss": l.loss(), "signal": self.signal(l.iface),
                       "quality": l.quality(self.signal(l.iface))} for l in self.links.values()],
        }

    async def send_report(self, force=False):
        r = self.report()
        key = {k: v for k, v in r.items() if k not in ("rtt", "links", "traffic")}
        key["q"] = [(l["up"], l["quality"]) for l in r["links"]]
        key.pop("loss"); key.pop("signal")
        old_rtt = (self.last_report or {}).get("rtt")
        rtt_moved = (r["rtt"] is None) != (old_rtt is None) or (
            r["rtt"] is not None and abs(r["rtt"] - old_rtt) > 30)
        now = time.monotonic()
        if force or key != (self.last_report or {}).get("_key") or (rtt_moved and now - self.last_report_t > 5) \
                or now - self.last_report_t > 10:
            self.last_report = {**r, "_key": key}
            self.last_report_t = now
            await self.on_report(r)

    def set_mode(self, mode):
        if mode in ("auto", "wifi", "hotspot") and mode != self.mode:
            self.mode = mode
            print("Network mode:", mode)

    # ---------- Probing ----------
    async def default_routes(self):
        """{iface: metric} for each interface that has a default route."""
        rc, out, _ = await run("ip", "-4", "-j", "route", "show", "default")
        routes = {}
        if rc == 0 and out.strip():
            for r in json.loads(out):
                dev = r.get("dev")
                if dev and dev != "lo":
                    m = r.get("metric", 0)
                    routes[dev] = min(m, routes.get(dev, m))
        return routes

    def probe(self, iface):
        """TCP connect to the server through one interface. Returns ms or None."""
        if not self.addr:
            return None
        s = socket.socket(self.addr[0], socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode())
            s.settimeout(PROBE_TIMEOUT_S)
            t = time.monotonic()
            s.connect(self.addr[1])
            return (time.monotonic() - t) * 1000
        except OSError as e:
            if isinstance(e, PermissionError) and "bind" not in self.warned:
                self.warned.add("bind")
                print("Network test needs Linux 5.7+ or CAP_NET_RAW:", e)
            return None
        finally:
            s.close()

    async def resolve(self):
        try:
            info = await asyncio.get_running_loop().getaddrinfo(
                self.host, self.port, type=socket.SOCK_STREAM, family=socket.AF_INET)
            self.addr = (info[0][0], info[0][4])
        except OSError:
            pass  # keep the last good address (DNS can fail on a dead link)

    async def update_ssid(self):
        if not HAS_NM:
            return
        rc, out, _ = await run("nmcli", "-t", "-f", "ACTIVE,SIGNAL,SSID", "device", "wifi", "list",
                               "ifname", WIFI_IFACE, "--rescan", "no", timeout=10)
        self.wifi_ssid, self.wifi_signal = "", None
        for line in out.splitlines():
            f = nm_fields(line)
            if len(f) >= 3 and f[0] == "yes":
                self.wifi_ssid, self.wifi_signal = f[2], int(f[1] or 0)

    # ---------- Choosing ----------
    def strong(self, l):
        """Wi-Fi good enough to drive on."""
        sig = self.signal(l.iface)
        return (l.usable() and (sig is None or sig >= WIFI_MIN_SIGNAL)
                and (l.loss() or 0) <= WIFI_MAX_LOSS and (l.rtt or 0) <= WIFI_MAX_RTT_MS)

    def update_strength(self):
        now = time.monotonic()
        for l in self.links.values():
            if self.strong(l):
                l.weak_since = None
                l.strong_since = l.strong_since or now
            else:
                l.strong_since = None
                l.weak_since = l.weak_since or now

    def choose(self):
        up = {i: l for i, l in self.links.items() if l.usable()}
        if not up:
            self.note = "No network works"
            return self.active
        want = "wifi" if self.mode in ("auto", "wifi") else "hotspot"
        cur = up.get(self.active)

        now = time.monotonic()

        def ok_wifi(l):
            """Auto: stay on Wi-Fi until it is weak for WIFI_POOR_S; move to it
            only when it is strong and stable."""
            if self.mode != "auto":
                return True
            if l is cur:
                return l.weak_since is None or now - l.weak_since < WIFI_POOR_S
            return l.strong_since is not None and now - l.strong_since >= WIFI_STABLE_S

        def best(kind, need_stable):
            c = [l for l in up.values() if self.kind(l.iface) == kind
                 and (not need_stable or l is cur or l.stable_for() >= WIFI_STABLE_S)
                 and (kind != "wifi" or not need_stable or ok_wifi(l))]
            return min(c, key=lambda l: l.rtt or 1e9).iface if c else None

        # Moving to Wi-Fi waits until it is strong and stable; leaving a dead link is at once
        pick = best(want, need_stable=(want == "wifi"))
        if pick:
            self.note = ""
            return self.active if cur and self.kind(self.active) == want else pick
        other = "hotspot" if want == "wifi" else "wifi"
        pick = best(other, need_stable=False)
        if self.mode != "auto":
            self.note = f"{'Wi-Fi' if want == 'wifi' else 'Hotspot'} is not working: using {other} instead"
        else:
            self.note = ""
        return self.active if cur and self.kind(self.active) == other else pick

    async def apply_routes(self, iface, routes):
        """Make iface the default route (lowest metric). True if routes changed.
        Only NetworkManager devices (Wi-Fi) are changed: 50 when in use, else 700.
        Others (eth0 on networkd, metric 100) keep their metric, so with two
        networks moving the Wi-Fi metric above or below 100 is enough."""
        if not HAS_NM:
            if "nonm" not in self.warned:
                self.warned.add("nonm")
                print("Cannot switch networks:", NO_NM)
            return False
        rc, out, _ = await run("nmcli", "-t", "-f", "DEVICE,STATE", "device", timeout=10)
        managed = {f[0] for f in map(nm_fields, out.splitlines()) if len(f) >= 2 and f[1] != "unmanaged"}
        changed = False
        for dev in routes:
            if dev not in managed:
                continue
            metric = ACTIVE_METRIC if dev == iface else IDLE_METRIC
            if routes[dev] == metric:
                continue
            rc, _, err = await run("nmcli", "device", "modify", dev, "ipv4.route-metric", str(metric))
            if rc != 0:
                if ("route" + dev) not in self.warned:
                    self.warned.add("route" + dev)
                    print(f"Cannot set route on {dev}: {nm_error(err)}")
            else:
                changed = True
        return changed

    async def run(self):
        print(f"Network monitor: Wi-Fi {WIFI_IFACE}, server {self.host}:{self.port}")
        while True:
            t0 = time.monotonic()
            try:
                await self.round()
            except Exception as e:
                print("Network monitor error:", repr(e))
            await asyncio.sleep(max(0.2, PROBE_S - (time.monotonic() - t0)))

    async def round(self):
        if not self.addr:
            await self.resolve()
        routes = await self.default_routes()
        await self.update_ssid()
        for dev in routes:
            self.links.setdefault(dev, Link(dev))
        for dev in list(self.links):
            if dev not in routes:
                del self.links[dev]
        if not self.links:
            self.active = None
            self.note = "No network"
            await self.send_report()
            return
        rtts = await asyncio.gather(*(asyncio.to_thread(self.probe, d) for d in self.links))
        now = time.monotonic()
        for l, rtt in zip(self.links.values(), rtts):
            l.hist.append(rtt is not None)
            if rtt is None:
                l.fails += 1
                if not l.usable():
                    l.ok_since, l.rtt = None, None
            else:
                if not l.usable() or l.ok_since is None:
                    l.ok_since = now
                l.fails, l.rtt = 0, rtt if l.rtt is None else l.rtt * 0.7 + rtt * 0.3
        self.update_strength()
        self.meter.update(self.kind)
        if self.active is None and routes:  # start: use what the system uses now
            self.active = min(routes, key=routes.get)
        new = self.choose() or self.active
        if new not in self.links:
            new = min(routes, key=routes.get)
        changed = await self.apply_routes(new, routes)
        if new != self.active:
            old, self.active = self.active, new
            print(f"Network: {old} -> {new} ({self.kind(new)})")
            await self.on_switch(old, new)
        elif changed:
            print(f"Network: {new} is the default route again")
        await self.send_report()
        if HAS_NM and WIFI_ROAM_S > 0 and not self.roaming and time.monotonic() >= self.roam_at:
            asyncio.create_task(self.roam())

    async def roam(self):
        """Connect to the strongest saved Wi-Fi. Only when Wi-Fi is weak or
        not in use, so a scan or switch does not disturb driving on good Wi-Fi."""
        self.roaming = True
        try:
            self.roam_at = time.monotonic() + WIFI_ROAM_S
            cur = self.wifi_signal if self.wifi_ssid else -1
            if self.active == WIFI_IFACE and cur >= WIFI_MIN_SIGNAL:
                return
            saved = await self.saved_wifi()
            saved = {k: v for k, v in saved.items() if k not in HOTSPOT_SSIDS}
            if not saved:
                return
            rc, out, _ = await run("nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list",
                                   "ifname", WIFI_IFACE, "--rescan", "yes", timeout=30)
            best, sig = None, -1
            for line in out.splitlines():
                f = nm_fields(line)
                if len(f) >= 2 and f[0] in saved and int(f[1] or 0) > sig:
                    best, sig = f[0], int(f[1] or 0)
            if best and best != self.wifi_ssid and sig >= max(WIFI_MIN_SIGNAL, cur + WIFI_ROAM_GAIN):
                print(f"Wi-Fi: moving to stronger {best} ({sig}%, was {self.wifi_ssid or 'none'} {max(cur, 0)}%)")
                rc, _, err = await run("nmcli", "-w", "30", "connection", "up", saved[best][0],
                                       "ifname", WIFI_IFACE, timeout=40)
                if rc != 0:
                    print(f"Wi-Fi: {best} failed: {nm_error(err)}")
        except Exception as e:
            print("Wi-Fi roam error:", repr(e))
        finally:
            self.roaming = False

    # ---------- Wi-Fi networks (web UI) ----------
    async def saved_wifi(self):
        """{ssid: [connection names]} of saved Wi-Fi networks."""
        rc, out, _ = await run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
        saved = {}
        for line in out.splitlines():
            f = nm_fields(line)
            if len(f) >= 2 and f[1] == "802-11-wireless":
                rc, ssid, _ = await run("nmcli", "-t", "-g", "802-11-wireless.ssid", "connection", "show", f[0])
                ssid = ssid.strip()
                if rc == 0 and ssid:
                    saved.setdefault(ssid, []).append(f[0])
        return saved

    async def scan(self):
        if not HAS_NM:
            return {"error": NO_NM}
        rc, out, err = await run("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi",
                                 "list", "ifname", WIFI_IFACE, "--rescan", "yes", timeout=30)
        if rc != 0:
            return {"error": "Scan failed: " + nm_error(err)}
        saved = await self.saved_wifi()
        nets = {}
        for line in out.splitlines():
            f = nm_fields(line)
            if len(f) < 4 or not f[1]:
                continue
            ssid, sig = f[1], int(f[2] or 0)
            n = nets.setdefault(ssid, {"ssid": ssid, "signal": 0, "security": f[3], "active": False})
            n["signal"] = max(n["signal"], sig)
            n["active"] = n["active"] or f[0] == "*"
        for n in nets.values():
            n["saved"] = n["ssid"] in saved
            n["hotspot"] = n["ssid"] in HOTSPOT_SSIDS
        self.scan_cache = nets
        return {"networks": sorted(nets.values(), key=lambda n: (not n["active"], not n["saved"], -n["signal"]))}

    async def connect_wifi(self, ssid, password, identity):
        if not HAS_NM:
            return {"ok": False, "msg": NO_NM}
        sec = self.scan_cache.get(ssid, {}).get("security", "")
        enterprise = "802.1X" in sec or "EAP" in sec
        names = (await self.saved_wifi()).get(ssid, [])
        if names:
            name = names[0]
            if password:
                if enterprise:
                    args = ["802-1x.password", password] + (["802-1x.identity", identity] if identity else [])
                else:
                    args = ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
                rc, _, err = await run("nmcli", "connection", "modify", name, *args)
                if rc != 0:
                    return {"ok": False, "msg": "Could not save password: " + nm_error(err)}
            rc, _, err = await run("nmcli", "-w", "40", "connection", "up", name, "ifname", WIFI_IFACE, timeout=50)
        elif enterprise:
            if not identity or not password:
                return {"ok": False, "msg": "School Wi-Fi needs a username and password"}
            rc, _, err = await run("nmcli", "connection", "add", "type", "wifi", "ifname", WIFI_IFACE,
                                   "con-name", ssid, "ssid", ssid, "wifi-sec.key-mgmt", "wpa-eap",
                                   "802-1x.eap", "peap", "802-1x.phase2-auth", "mschapv2",
                                   "802-1x.identity", identity, "802-1x.password", password)
            if rc == 0:
                rc, _, err = await run("nmcli", "-w", "40", "connection", "up", ssid, timeout=50)
                if rc != 0:  # wrong login: do not keep a broken network
                    await run("nmcli", "connection", "delete", ssid)
        else:
            args = ["nmcli", "-w", "40", "device", "wifi", "connect", ssid, "ifname", WIFI_IFACE]
            if password:
                args += ["password", password]
            rc, _, err = await run(*args, timeout=50)
            if rc != 0:  # wrong password: NetworkManager keeps a broken profile
                for n in (await self.saved_wifi()).get(ssid, []):
                    await run("nmcli", "connection", "delete", n)
        if rc != 0:
            print(f"Wi-Fi connect to {ssid} failed: {nm_error(err)}")
            return {"ok": False, "msg": f"Could not connect to {ssid}: {nm_error(err)}"}
        print(f"Wi-Fi connected: {ssid}")
        return {"ok": True, "msg": f"Connected to {ssid}. The car uses it when it works for {WIFI_STABLE_S:g} s."}

    async def forget_wifi(self, ssid):
        if not HAS_NM:
            return {"ok": False, "msg": NO_NM}
        names = (await self.saved_wifi()).get(ssid, [])
        if not names:
            return {"ok": False, "msg": f"{ssid} is not saved"}
        for n in names:
            rc, _, err = await run("nmcli", "connection", "delete", n)
            if rc != 0:
                return {"ok": False, "msg": "Could not forget: " + nm_error(err)}
        print(f"Wi-Fi forgotten: {ssid}")
        return {"ok": True, "msg": f"Forgot {ssid}"}
