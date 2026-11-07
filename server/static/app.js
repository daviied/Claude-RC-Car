const socket = io();
const $ = (id) => document.getElementById(id);

function setPill(id, text, ok) {
  $(id).textContent = text;
  $(id).className = 'pill ' + (ok ? 'on' : 'off');
}

// ---------- WebRTC video ----------
let pc = null;

async function startVideo() {
  if (pc) pc.close();
  setPill('video', 'Video: connecting', false);

  const iceServers = await socket.emitWithAck('get_ice');
  pc = new RTCPeerConnection({ iceServers });
  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.ontrack = (e) => { $('cam').srcObject = e.streams[0] || new MediaStream([e.track]); };
  pc.onconnectionstatechange = () => {
    const s = pc.connectionState;
    setPill('video', 'Video: ' + s, s === 'connected');
    if (s === 'failed') setTimeout(startVideo, 3000);
  };

  await pc.setLocalDescription(await pc.createOffer());
  // aiortc does not use trickle ICE: wait for all candidates (max 3 s)
  await new Promise((resolve) => {
    if (pc.iceGatheringState === 'complete') return resolve();
    pc.onicegatheringstatechange = () => pc.iceGatheringState === 'complete' && resolve();
    setTimeout(resolve, 3000);
  });
  socket.emit('webrtc_offer', { sdp: pc.localDescription.sdp, type: pc.localDescription.type });
}

socket.on('webrtc_answer', (d) => pc && pc.setRemoteDescription(d));
socket.on('webrtc_error', (msg) => setPill('video', 'Video: ' + msg, false));
socket.on('car_status', (online) => {
  setPill('car', online ? 'Car online' : 'Car offline', online);
  if (online) startVideo();
});
socket.on('connect_error', (err) => {
  if (/rejected/i.test(err.message)) location.href = '/login'; // session expired
});

// ---------- Controls ----------
const send = (k) => socket.emit('Key_Press', k);

const Action = {
  W_Down() { send('W_Down'); }, W_Up() { send('W_Up'); },
  A_Down() { send('A_Down'); }, A_Up() { send('A_Up'); },
  S_Down() { send('S_Down'); }, S_Up() { send('S_Up'); },
  D_Down() { send('D_Down'); }, D_Up() { send('D_Up'); },
  Q_Down() { send('Q_Down'); }, Q_Up() { send('Q_Up'); },
  E_Down() { send('E_Down'); }, E_Up() { send('E_Up'); },
  G_Down() { send('G_Down'); }, G_Up() { send('G_Up'); },
  H_Down() { send('H_Down'); }, H_Up() { send('H_Up'); },
};

const keyAction = {
  w: { keydown: Action.W_Down, keyup: Action.W_Up },
  a: { keydown: Action.A_Down, keyup: Action.A_Up },
  s: { keydown: Action.S_Down, keyup: Action.S_Up },
  d: { keydown: Action.D_Down, keyup: Action.D_Up },
  q: { keydown: Action.Q_Down, keyup: Action.Q_Up },
  e: { keydown: Action.E_Down, keyup: Action.E_Up },
  g: { keydown: Action.G_Down, keyup: Action.G_Up },
  h: { keydown: Action.H_Down, keyup: Action.H_Up },
  ArrowUp: { keydown: Action.W_Down, keyup: Action.W_Up },
  ArrowLeft: { keydown: Action.A_Down, keyup: Action.A_Up },
  ArrowDown: { keydown: Action.S_Down, keyup: Action.S_Up },
  ArrowRight: { keydown: Action.D_Down, keyup: Action.D_Up },
};

// Track held keys so a key is never "down" twice or "up" without "down"
const held = new Set();
function press(key, type) {
  if (!(key in keyAction)) return;
  if (type === 'keydown' && held.has(key)) return;
  if (type === 'keyup' && !held.has(key)) return;
  type === 'keydown' ? held.add(key) : held.delete(key);
  keyAction[key][type]();
}

['keydown', 'keyup'].forEach((t) =>
  document.addEventListener(t, (ev) => {
    const key = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
    if (key in keyAction) ev.preventDefault();
    press(key, t);
  })
);

// Touch / mouse buttons (mobile)
document.querySelectorAll('#pad button').forEach((btn) => {
  const k = btn.dataset.key;
  btn.addEventListener('pointerdown', (e) => { e.preventDefault(); btn.setPointerCapture(e.pointerId); press(k, 'keydown'); });
  ['pointerup', 'pointercancel'].forEach((t) => btn.addEventListener(t, () => press(k, 'keyup')));
  btn.addEventListener('contextmenu', (e) => e.preventDefault());
});

// ---------- Game controller (Gamepad API, "standard" layout) ----------
// RT = forward, LT = reverse (analog). Left stick Y also works for throttle.
// Left stick X = steering. A/B/X/Y = Q/E/G/H actions.
const DEADZONE = 0.12;
const PAD_BUTTONS = { 0: 'q', 1: 'e', 2: 'g', 3: 'h' };
const padHeld = new Set();
let padIndex = null;
let lastPad = [0, 0];
let lastPadSent = 0;

const dz = (v) => (Math.abs(v) < DEADZONE ? 0 : (v - Math.sign(v) * DEADZONE) / (1 - DEADZONE));
const r2 = (v) => Math.round(v * 100) / 100;

window.addEventListener('gamepadconnected', (e) => {
  padIndex = e.gamepad.index;
  setPill('gp', 'Controller', true);
});
window.addEventListener('gamepaddisconnected', (e) => {
  if (e.gamepad.index !== padIndex) return;
  padIndex = null;
  padHeld.forEach((k) => press(k, 'keyup'));
  padHeld.clear();
  socket.emit('Pad', [0, 0]);
  lastPad = [0, 0];
  setPill('gp', 'No controller', false);
});

function pollPad() {
  requestAnimationFrame(pollPad);
  if (padIndex === null) return;
  const gp = navigator.getGamepads()[padIndex];
  if (!gp) return;

  const rt = gp.buttons[7] ? gp.buttons[7].value : 0;
  const lt = gp.buttons[6] ? gp.buttons[6].value : 0;
  let throttle = rt - lt;
  if (Math.abs(throttle) < 0.05) throttle = -dz(gp.axes[1] || 0);
  const steering = dz(gp.axes[0] || 0);
  const v = [r2(throttle), r2(steering)];

  // Send on change (max 30/s), and resend every 100 ms so state is never stale
  const now = performance.now();
  const changed = v[0] !== lastPad[0] || v[1] !== lastPad[1];
  if ((changed && now - lastPadSent > 33) || now - lastPadSent > 100) {
    socket.emit('Pad', v);
    lastPad = v;
    lastPadSent = now;
  }

  for (const [i, key] of Object.entries(PAD_BUTTONS)) {
    const down = gp.buttons[i] && gp.buttons[i].pressed;
    if (down && !padHeld.has(key)) { padHeld.add(key); press(key, 'keydown'); }
    if (!down && padHeld.has(key)) { padHeld.delete(key); press(key, 'keyup'); }
  }
}
requestAnimationFrame(pollPad);

// Stop if the page loses focus (keyup events would be lost)
function stopAll() { held.clear(); send('Stop'); }
window.addEventListener('blur', stopAll);
document.addEventListener('visibilitychange', () => document.hidden && stopAll());

// Latency display
setInterval(async () => {
  if (!socket.connected) return;
  const t = performance.now();
  try {
    await socket.timeout(2000).emitWithAck('my_ping');
    $('rtt').textContent = Math.round(performance.now() - t) + ' ms';
  } catch { $('rtt').textContent = 'timeout'; }
}, 1000);
// Heartbeat: keeps the car failsafe alive (car stops after 0.5 s without one)
setInterval(() => socket.connected && send('Heartbeat'), 200);

// ---------- PWA ----------
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
