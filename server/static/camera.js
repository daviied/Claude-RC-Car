// Phone camera source: streams a USB webcam (or the phone camera) to the
// drive page over WebRTC. Video uses the phone's own data, not the car's hotspot.
const socket = io({ auth: { role: 'phonecam' } });
const $ = (id) => document.getElementById(id);
const QUALITY = {
  480: { width: 640, height: 480, bitrate: 1_000_000 },
  720: { width: 1280, height: 720, bitrate: 2_500_000 },
  1080: { width: 1920, height: 1080, bitrate: 4_000_000 },
};
const peers = new Map(); // viewer socket id -> RTCPeerConnection
let stream = null;

function setPill(id, text, ok) {
  $(id).textContent = text;
  $(id).className = 'pill ' + (ok ? 'on' : 'off');
}
const say = (t) => { $('msg').textContent = t; };
const showViewers = () => { $('viewers').textContent = peers.size + (peers.size === 1 ? ' viewer' : ' viewers'); };

// ---------- Camera ----------
async function listCameras() {
  const sel = $('camsel');
  let saved = '';
  try { saved = localStorage.getItem('rc-cam') || ''; } catch {}
  const keep = sel.value || saved;
  const cams = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput');
  sel.innerHTML = '';
  cams.forEach((c, i) => {
    const o = document.createElement('option');
    o.value = c.deviceId;
    o.textContent = c.label || `Camera ${i + 1}`;
    sel.appendChild(o);
  });
  // Prefer the saved camera, else a USB/external one
  const ext = cams.find((c) => /usb|uvc|external|webcam/i.test(c.label));
  if (cams.some((c) => c.deviceId === keep)) sel.value = keep;
  else if (ext) sel.value = ext.deviceId;
}

async function startCamera() {
  const q = QUALITY[$('quality').value];
  const id = $('camsel').value;
  try {
    const s = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { deviceId: id ? { exact: id } : undefined, width: { ideal: q.width }, height: { ideal: q.height }, frameRate: { ideal: 30 } },
    });
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = s;
    $('cam').srcObject = stream;
    const track = stream.getVideoTracks()[0];
    track.contentHint = 'motion';
    const set = track.getSettings();
    say(`Camera: ${track.label || 'on'} (${set.width}x${set.height})`);
    track.onended = () => { say('Camera disconnected. Plug it in again.'); setTimeout(restartCamera, 2000); };
    // Live viewers switch to the new camera without reconnecting
    for (const pc of peers.values()) {
      pc.getSenders().forEach((snd) => snd.replaceTrack(track));
      applyBitrate(pc);
    }
  } catch (e) {
    say('Camera error: ' + e.message + '. Allow camera access, and check the USB cable.');
  }
}

async function restartCamera() {
  try { localStorage.setItem('rc-cam', $('camsel').value); } catch {}
  await listCameras();
  await startCamera();
}

async function applyBitrate(pc) {
  const max = QUALITY[$('quality').value].bitrate;
  for (const snd of pc.getSenders()) {
    if (snd.track?.kind !== 'video') continue;
    const p = snd.getParameters();
    if (!p.encodings || !p.encodings.length) p.encodings = [{}];
    p.encodings[0].maxBitrate = max;
    p.degradationPreference = 'maintain-framerate';
    try { await snd.setParameters(p); } catch {}
  }
}

$('camsel').onchange = restartCamera;
$('quality').onchange = startCamera;
navigator.mediaDevices.addEventListener('devicechange', () => listCameras());

// ---------- WebRTC (answer offers from drive pages) ----------
function closePeer(sid) {
  const pc = peers.get(sid);
  if (pc) pc.close();
  peers.delete(sid);
  showViewers();
}

socket.on('webrtc_offer', async ({ sid, sdp, type }) => {
  closePeer(sid);
  if (!stream) { say('Viewer asked for video, but the camera is not on.'); return; }
  const iceServers = await socket.emitWithAck('get_ice');
  const pc = new RTCPeerConnection({ iceServers });
  peers.set(sid, pc);
  showViewers();
  pc.onconnectionstatechange = () => {
    if (['failed', 'closed'].includes(pc.connectionState) && peers.get(sid) === pc) closePeer(sid);
  };
  stream.getVideoTracks().forEach((t) => pc.addTrack(t, stream));
  await pc.setRemoteDescription({ sdp, type });
  await pc.setLocalDescription(await pc.createAnswer());
  // The drive page waits for full ICE gathering too (same flow as the car)
  await new Promise((resolve) => {
    if (pc.iceGatheringState === 'complete') return resolve();
    pc.onicegatheringstatechange = () => pc.iceGatheringState === 'complete' && resolve();
    setTimeout(resolve, 3000);
  });
  if (peers.get(sid) !== pc) return;
  socket.emit('webrtc_answer', { sid, sdp: pc.localDescription.sdp, type: pc.localDescription.type });
  applyBitrate(pc);
});

socket.on('viewer_left', closePeer);
socket.on('video_stop', () => { [...peers.keys()].forEach(closePeer); });
socket.on('video_status', ({ source }) => {
  const on = source === 'phone';
  setPill('mode', on ? 'Video source: this phone' : 'Not selected (car camera in use)', on);
});
socket.on('replaced', () => { say('Another phone camera page took over.'); socket.disconnect(); });
socket.on('connect', () => setPill('srv', 'Server: online', true));
socket.on('disconnect', () => { setPill('srv', 'Server: offline', false); [...peers.keys()].forEach(closePeer); });
socket.on('connect_error', (err) => { if (/rejected/i.test(err.message)) location.href = '/login'; });

// ---------- Keep the phone awake ----------
let wakeLock = null;
async function keepAwake() {
  if (!('wakeLock' in navigator) || document.hidden || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch {}
}
document.addEventListener('visibilitychange', keepAwake);
document.addEventListener('pointerdown', keepAwake);

(async () => {
  if (!navigator.mediaDevices?.getUserMedia) { say('This browser cannot use cameras (needs HTTPS).'); return; }
  await listCameras();
  await startCamera();
  // Names are only shown after camera permission: list again, and switch
  // to the USB webcam if the browser opened a different camera first
  await listCameras();
  const active = stream?.getVideoTracks()[0]?.getSettings().deviceId;
  if (active && $('camsel').value && $('camsel').value !== active) await startCamera();
  keepAwake();
})();
