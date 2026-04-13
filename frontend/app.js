/**
 * app.js — MeetFree Client (v3)
 * New: Lobby/Waiting Room + Admin Admit/Deny/Kick
 */

// ── Bootstrap ─────────────────────────────────────────────────────────────────
const _token     = sessionStorage.getItem('meetfree_token');
const _iceRaw    = sessionStorage.getItem('meetfree_ice');
const _savedName = sessionStorage.getItem('meetfree_name');

if (!_token) {
  window.location.href = '/?room=' + encodeURIComponent(ROOM_ID);
}

const ICE_CONFIG = {
  iceServers: _iceRaw ? JSON.parse(_iceRaw) : [
    { urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302'] }
  ]
};

// ── State ─────────────────────────────────────────────────────────────────────
let localStream     = null;
let screenStream    = null;
let localName       = _savedName || 'Me';
let isMicOn         = true;
let isCamOn         = true;
let isScreenSharing = false;
let isHandRaised    = false;
let isAdmin         = false;      // true if this user is the room host
let unreadChats     = 0;
let lobbyWaiters    = {};         // { peerId: name } — people waiting in lobby

const peerConnections = {};
const peerTiles       = {};
const peerNames       = {};

// ── Socket.IO ─────────────────────────────────────────────────────────────────
const socket = io({ query: { token: _token }, reconnectionAttempts: 5 });

socket.on('connect_error', (err) => {
  if (err.message.includes('auth') || err.message.includes('401')) {
    sessionStorage.clear();
    window.location.href = '/?room=' + encodeURIComponent(ROOM_ID) + '&error=session_expired';
  }
});

socket.on('connect', () => {
  document.getElementById('authStatus').textContent = 'Connected. Loading room…';
  document.getElementById('authGuard').classList.add('hidden');
  document.getElementById('preCallModal').classList.remove('hidden');
  if (_savedName) document.getElementById('localNameDisplay').textContent = localName;
  startPreview();
});


// ── Pre-Call Preview ──────────────────────────────────────────────────────────
let previewStream     = null;
let previewCamEnabled = true;
let previewMicEnabled = true;

async function startPreview() {
  try {
    previewStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
    const preview = document.getElementById('previewVideo');
    preview.srcObject = previewStream;
    document.getElementById('previewPlaceholder').style.display = 'none';
    preview.style.display = 'block';
  } catch(e) { console.warn('[PREVIEW] Camera unavailable:', e.name); }
}

function togglePreviewCamera() {
  if (!previewStream) return;
  previewCamEnabled = !previewCamEnabled;
  previewStream.getVideoTracks().forEach(t => (t.enabled = previewCamEnabled));
  document.getElementById('previewCamIcon').textContent = previewCamEnabled ? '📷' : '🚫';
  document.getElementById('previewVideo').style.display = previewCamEnabled ? 'block' : 'none';
  document.getElementById('previewPlaceholder').style.display = previewCamEnabled ? 'none' : 'flex';
}

function togglePreviewMic() {
  if (!previewStream) return;
  previewMicEnabled = !previewMicEnabled;
  previewStream.getAudioTracks().forEach(t => (t.enabled = previewMicEnabled));
  document.getElementById('previewMicIcon').textContent = previewMicEnabled ? '🎙️' : '🔇';
}


// ── Join Room ─────────────────────────────────────────────────────────────────
async function enterRoom() {
  if (previewStream) previewStream.getTracks().forEach(t => t.stop());

  try {
    localStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
  } catch(e) {
    try { localStream = await navigator.mediaDevices.getUserMedia({ video: false, audio: true }); isCamOn = false; }
    catch(e2) { showToast('Could not access media devices.'); return; }
  }

  if (!previewCamEnabled) { localStream.getVideoTracks().forEach(t => (t.enabled = false)); isCamOn = false; }
  if (!previewMicEnabled) { localStream.getAudioTracks().forEach(t => (t.enabled = false)); isMicOn = false; }

  document.getElementById('preCallModal').classList.add('hidden');

  // Emit request_join — server decides: admit immediately (empty room) or put in lobby
  socket.emit('request_join', { room: ROOM_ID, name: localName });
}

// Called once the server admits us (either immediately as first user, or after admin approval)
function onAdmittedToRoom(isAdminUser) {
  isAdmin = isAdminUser;

  document.getElementById('lobbyScreen').classList.add('hidden');
  document.getElementById('roomContainer').classList.remove('hidden');
  document.getElementById('controlBar').classList.remove('hidden');

  addLocalTile();
  updateControlBarState();
  startQualityMonitor();

  // Show the lobby management button only to the admin
  if (isAdmin) {
    document.getElementById('lobbyBtn').classList.remove('hidden');
    showToast('👑 You are the host of this room');
  }
}


// ── Lobby: Waiting screen (for non-admin users) ───────────────────────────────

// Server says "wait for approval"
socket.on('waiting_for_approval', ({ message }) => {
  document.getElementById('preCallModal').classList.add('hidden');
  document.getElementById('lobbyScreen').classList.remove('hidden');
  document.getElementById('lobbyMessage').textContent = message;
});

// Server tells us the result of the admin's decision
socket.on('admission_result', ({ admitted, message }) => {
  if (admitted) {
    // onAdmittedToRoom is called via room_peers event which follows
    showToast('✅ ' + message);
  } else {
    document.getElementById('lobbyScreen').classList.add('hidden');
    document.getElementById('deniedScreen').classList.remove('hidden');
  }
});

// Admin removed us from the room
socket.on('you_were_removed', ({ message }) => {
  showToast('🚫 ' + message, 5000);
  setTimeout(() => { window.location.href = '/'; }, 2500);
});

function leaveLobby() {
  socket.disconnect();
  window.location.href = '/';
}


// ── Lobby: Admin Panel ────────────────────────────────────────────────────────

// Server tells the admin someone is knocking
socket.on('lobby_request', ({ peerId, name }) => {
  lobbyWaiters[peerId] = name;
  updateLobbyPanel();
  // Flash the lobby button badge
  const badge = document.getElementById('lobbyBadge');
  badge.textContent = Object.keys(lobbyWaiters).length;
  badge.classList.remove('hidden');
  showToast('🚪 ' + name + ' is waiting to join', 5000);
});

// Admin became admin (e.g. original admin left)
socket.on('you_are_admin', () => {
  isAdmin = true;
  document.getElementById('lobbyBtn').classList.remove('hidden');
  showToast('👑 You are now the host');
});

function admitUser(peerId) {
  socket.emit('admit_user', { peerId });
  delete lobbyWaiters[peerId];
  updateLobbyPanel();
}

function denyUser(peerId) {
  socket.emit('deny_user', { peerId });
  delete lobbyWaiters[peerId];
  updateLobbyPanel();
}

function removeParticipant(peerId) {
  if (!confirm('Remove ' + (peerNames[peerId] || 'this participant') + ' from the meeting?')) return;
  socket.emit('remove_participant', { peerId });
}

function updateLobbyPanel() {
  const list    = document.getElementById('lobbyList');
  const empty   = document.getElementById('lobbyEmpty');
  const badge   = document.getElementById('lobbyBadge');
  const count   = document.getElementById('lobbyCount');
  const waiters = Object.entries(lobbyWaiters);

  count.textContent = waiters.length;
  badge.textContent = waiters.length;
  badge.classList.toggle('hidden', waiters.length === 0);

  // Clear existing waiter rows (keep the empty message)
  list.querySelectorAll('.lobby-waiter-row').forEach(el => el.remove());

  if (waiters.length === 0) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  waiters.forEach(([peerId, name]) => {
    const row = document.createElement('div');
    row.className = 'lobby-waiter-row';
    row.id = 'waiter-' + peerId;
    row.innerHTML =
      '<span class="waiter-name">👤 ' + escapeHtml(name) + '</span>' +
      '<div class="waiter-actions">' +
        '<button class="admit-btn" onclick="admitUser(\'' + peerId + '\')">Admit</button>' +
        '<button class="deny-btn"  onclick="denyUser(\''  + peerId + '\')">Deny</button>' +
      '</div>';
    list.appendChild(row);
  });
}

function toggleLobbyPanel() {
  const panel = document.getElementById('lobbyPanel');
  panel.classList.toggle('hidden');
  if (!panel.classList.contains('hidden')) {
    document.getElementById('lobbyBadge').classList.add('hidden');
  }
}


// ── Video Grid ────────────────────────────────────────────────────────────────
function addLocalTile() {
  document.getElementById('videoGrid').appendChild(
    createTile('local', localName + ' (You)', localStream, true)
  );
  updateGridLayout();
  updateParticipantCount();
}

function createTile(id, label, stream, muted) {
  const container       = document.createElement('div');
  container.className   = 'video-tile';
  container.id          = 'tile-' + id;

  const video           = document.createElement('video');
  video.autoplay = true; video.playsInline = true; video.muted = !!muted;
  if (stream) video.srcObject = stream;

  const placeholder     = document.createElement('div');
  placeholder.className = 'video-placeholder';
  placeholder.id        = 'placeholder-' + id;
  placeholder.innerHTML = '<span>👤</span><p>' + escapeHtml(label) + '</p>';

  const nameTag         = document.createElement('div');
  nameTag.className     = 'name-tag';
  nameTag.id            = 'nametag-' + id;
  nameTag.textContent   = label;

  const muteIcon  = document.createElement('div');
  muteIcon.className  = 'tile-overlay-icon mute-icon hidden';
  muteIcon.textContent = '🔇';
  muteIcon.id = 'mute-' + id;

  const camOffIcon = document.createElement('div');
  camOffIcon.className  = 'tile-overlay-icon camoff-icon hidden';
  camOffIcon.textContent = '🚫';
  camOffIcon.id = 'camoff-' + id;

  const handIcon  = document.createElement('div');
  handIcon.className  = 'tile-overlay-icon hand-icon hidden';
  handIcon.textContent = '✋';
  handIcon.id = 'hand-' + id;

  // Admin: show remove button on remote tiles
  if (id !== 'local' && isAdmin) {
    const kickBtn = document.createElement('button');
    kickBtn.className   = 'tile-kick-btn';
    kickBtn.textContent = '✕ Remove';
    kickBtn.title       = 'Remove from meeting';
    kickBtn.onclick     = () => removeParticipant(id);
    container.appendChild(kickBtn);
  }

  container.appendChild(video);
  container.appendChild(placeholder);
  container.appendChild(nameTag);
  container.appendChild(muteIcon);
  container.appendChild(camOffIcon);
  container.appendChild(handIcon);

  const hasVideo = stream && stream.getVideoTracks().length > 0 && stream.getVideoTracks()[0].enabled;
  video.style.display       = hasVideo ? 'block' : 'none';
  placeholder.style.display = hasVideo ? 'none'  : 'flex';

  peerTiles[id] = { videoEl: video, placeholder, nameTag, muteIcon, camOffIcon, handIcon };
  return container;
}

function updateGridLayout() {
  const grid  = document.getElementById('videoGrid');
  const count = grid.children.length;
  grid.className = 'video-grid';
  if      (count === 1) grid.classList.add('grid-1');
  else if (count === 2) grid.classList.add('grid-2');
  else if (count <= 4)  grid.classList.add('grid-4');
  else                  grid.classList.add('grid-many');
}

function removeTile(peerId) {
  const tile = document.getElementById('tile-' + peerId);
  if (tile) tile.remove();
  delete peerTiles[peerId];
  delete peerNames[peerId];
  updateGridLayout();
  updateParticipantCount();
}

function updateParticipantCount() {
  const el = document.getElementById('participantNum');
  if (el) el.textContent = document.getElementById('videoGrid').children.length;
}


// ── Peer Connections ──────────────────────────────────────────────────────────
function createPeerConnection(peerId, isInitiator) {
  const pc = new RTCPeerConnection(ICE_CONFIG);
  peerConnections[peerId] = pc;

  if (localStream) localStream.getTracks().forEach(t => pc.addTrack(t, localStream));

  pc.ontrack = (event) => {
    const [remoteStream] = event.streams;
    if (!peerTiles[peerId]) return;
    peerTiles[peerId].videoEl.srcObject = remoteStream;
    if (event.track.kind === 'video') {
      peerTiles[peerId].videoEl.style.display     = 'block';
      peerTiles[peerId].placeholder.style.display = 'none';
    }
  };

  pc.onicecandidate = (e) => {
    if (e.candidate) socket.emit('ice_candidate', { target: peerId, candidate: e.candidate });
  };

  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'disconnected' || pc.connectionState === 'failed')
      handlePeerDisconnect(peerId);
    if (pc.connectionState === 'connected')
      showToast('✅ Connected to ' + (peerNames[peerId] || 'a participant'));
  };

  if (isInitiator) {
    pc.createOffer()
      .then(o => pc.setLocalDescription(o))
      .then(() => socket.emit('offer', { target: peerId, sdp: pc.localDescription }))
      .catch(e => console.error('[OFFER]', e));
  }
  return pc;
}

function handlePeerDisconnect(peerId) {
  if (peerConnections[peerId]) { peerConnections[peerId].close(); delete peerConnections[peerId]; }
  removeTile(peerId);
  showToast('👋 ' + (peerNames[peerId] || 'A participant') + ' left');
}


// ── Signaling Events ──────────────────────────────────────────────────────────

// room_peers fires once we are admitted into the actual room
socket.on('room_peers', ({ peers, isAdmin: adminFlag }) => {
  onAdmittedToRoom(!!adminFlag);
  peers.forEach(({ peerId, name }) => {
    peerNames[peerId] = name;
    document.getElementById('videoGrid').appendChild(createTile(peerId, name, null, false));
    updateGridLayout();
    updateParticipantCount();
    createPeerConnection(peerId, true);
  });
});

socket.on('peer_joined', ({ peerId, name }) => {
  peerNames[peerId] = name;
  showToast('👋 ' + name + ' joined');
  document.getElementById('videoGrid').appendChild(createTile(peerId, name, null, false));
  updateGridLayout();
  updateParticipantCount();
});

socket.on('offer', async ({ sdp, caller }) => {
  const pc = createPeerConnection(caller, false);
  await pc.setRemoteDescription(new RTCSessionDescription(sdp));
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  socket.emit('answer', { target: caller, sdp: pc.localDescription });
});

socket.on('answer', async ({ sdp, answerer }) => {
  const pc = peerConnections[answerer];
  if (pc) await pc.setRemoteDescription(new RTCSessionDescription(sdp));
});

socket.on('ice_candidate', async ({ candidate, sender }) => {
  const pc = peerConnections[sender];
  if (pc && candidate) {
    try { await pc.addIceCandidate(new RTCIceCandidate(candidate)); } catch(e) {}
  }
});

socket.on('peer_left', ({ peerId }) => handlePeerDisconnect(peerId));

socket.on('chat_message', ({ message, sender }) => {
  appendChatMessage(sender, message, false);
  const panel = document.getElementById('chatPanel');
  if (panel.classList.contains('hidden')) {
    panel.classList.remove('hidden');
    unreadChats = 0;
    document.getElementById('chatBadge').classList.add('hidden');
  }
  const preview = message.length > 45 ? message.slice(0,45) + '...' : message;
  showToast(sender + ': ' + preview, 4000);
});

socket.on('hand_raised', ({ peerId, name, raised }) => {
  if (peerTiles[peerId]) peerTiles[peerId].handIcon.classList.toggle('hidden', !raised);
  if (raised) showToast('✋ ' + name + ' raised their hand', 4000);
});

socket.on('peer_media_state', ({ peerId, audioOn, videoOn }) => {
  const tile = peerTiles[peerId];
  if (!tile) return;
  tile.muteIcon.classList.toggle('hidden', audioOn);
  if (!videoOn) {
    tile.videoEl.style.display       = 'none';
    tile.placeholder.style.display   = 'flex';
    tile.camOffIcon.classList.remove('hidden');
  } else {
    tile.camOffIcon.classList.add('hidden');
    if (tile.videoEl.srcObject) { tile.videoEl.style.display = 'block'; tile.placeholder.style.display = 'none'; }
  }
});


// ── Controls ──────────────────────────────────────────────────────────────────
function toggleMic() {
  isMicOn = !isMicOn;
  if (localStream) localStream.getAudioTracks().forEach(t => (t.enabled = isMicOn));
  if (peerTiles['local']) peerTiles['local'].muteIcon.classList.toggle('hidden', isMicOn);
  socket.emit('media_state', { audioOn: isMicOn, videoOn: isCamOn });
  updateControlBarState();
}

function toggleCamera() {
  isCamOn = !isCamOn;
  if (localStream) localStream.getVideoTracks().forEach(t => (t.enabled = isCamOn));
  if (peerTiles['local']) {
    peerTiles['local'].videoEl.style.display     = isCamOn ? 'block' : 'none';
    peerTiles['local'].placeholder.style.display = isCamOn ? 'none'  : 'flex';
    peerTiles['local'].camOffIcon.classList.toggle('hidden', isCamOn);
  }
  socket.emit('media_state', { audioOn: isMicOn, videoOn: isCamOn });
  updateControlBarState();
}

async function toggleScreenShare() {
  if (!isScreenSharing) {
    try {
      screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true });
      const track  = screenStream.getVideoTracks()[0];
      Object.values(peerConnections).forEach(pc => {
        const s = pc.getSenders().find(s => s.track && s.track.kind === 'video');
        if (s) s.replaceTrack(track);
      });
      if (peerTiles['local']) peerTiles['local'].videoEl.srcObject = screenStream;
      isScreenSharing = true;
      document.getElementById('screenIcon').textContent = '⏹️';
      document.getElementById('screenBtn').classList.add('active');
      showToast('🖥️ Screen sharing started');
      track.onended = stopScreenShare;
    } catch(e) {}
  } else { stopScreenShare(); }
}

function stopScreenShare() {
  if (!screenStream) return;
  screenStream.getTracks().forEach(t => t.stop());
  isScreenSharing = false;
  const camTrack = localStream && localStream.getVideoTracks()[0];
  if (camTrack) {
    Object.values(peerConnections).forEach(pc => {
      const s = pc.getSenders().find(s => s.track && s.track.kind === 'video');
      if (s) s.replaceTrack(camTrack);
    });
    if (peerTiles['local']) peerTiles['local'].videoEl.srcObject = localStream;
  }
  document.getElementById('screenIcon').textContent = '🖥️';
  document.getElementById('screenBtn').classList.remove('active');
  showToast('🖥️ Screen sharing stopped');
}

function toggleHand() {
  isHandRaised = !isHandRaised;
  document.getElementById('handBtn').classList.toggle('active', isHandRaised);
  if (peerTiles['local']) peerTiles['local'].handIcon.classList.toggle('hidden', !isHandRaised);
  socket.emit('raise_hand', { raised: isHandRaised });
  if (isHandRaised) showToast('✋ You raised your hand');
}

function leaveCall() {
  Object.values(peerConnections).forEach(pc => pc.close());
  if (localStream) localStream.getTracks().forEach(t => t.stop());
  if (screenStream) screenStream.getTracks().forEach(t => t.stop());
  sessionStorage.removeItem('meetfree_token');
  sessionStorage.removeItem('meetfree_ice');
  socket.disconnect();
  window.location.href = '/';
}

function updateControlBarState() {
  document.getElementById('micIcon').textContent = isMicOn ? '🎙️' : '🔇';
  document.getElementById('micBtn').className    = 'ctrl-btn ' + (isMicOn ? 'active' : 'muted');
  document.getElementById('camIcon').textContent = isCamOn ? '📷' : '🚫';
  document.getElementById('camBtn').className    = 'ctrl-btn ' + (isCamOn ? 'active' : 'muted');
}


// ── Chat ──────────────────────────────────────────────────────────────────────
function toggleChat() {
  const panel = document.getElementById('chatPanel');
  panel.classList.toggle('hidden');
  if (!panel.classList.contains('hidden')) {
    unreadChats = 0;
    document.getElementById('chatBadge').classList.add('hidden');
    document.getElementById('chatInput').focus();
  }
}

function sendChat() {
  const input = document.getElementById('chatInput');
  const msg   = input.value.trim();
  if (!msg) return;
  socket.emit('chat_message', { room: ROOM_ID, message: msg, name: localName });
  appendChatMessage('You', msg, true);
  input.value = '';
}

function appendChatMessage(sender, message, isSelf) {
  const container = document.getElementById('chatMessages');
  const el        = document.createElement('div');
  el.className    = 'chat-msg ' + (isSelf ? 'self' : 'other');
  const ts        = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  el.innerHTML    =
    '<span class="chat-sender">' + escapeHtml(sender)  + '</span>' +
    '<span class="chat-text">'   + escapeHtml(message) + '</span>' +
    '<span class="chat-ts">'     + ts                  + '</span>';
  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
}


// ── Quality Monitor ───────────────────────────────────────────────────────────
let qualityInterval = null;
function startQualityMonitor() {
  qualityInterval = setInterval(async () => {
    const pcs = Object.values(peerConnections);
    if (!pcs.length) return;
    const pc = pcs[0];
    if (pc.connectionState !== 'connected') return;
    try {
      const stats = await pc.getStats();
      let rtt = null;
      stats.forEach(r => {
        if (r.type === 'candidate-pair' && r.state === 'succeeded' && r.currentRoundTripTime !== undefined)
          rtt = r.currentRoundTripTime * 1000;
      });
      const icon = document.getElementById('qualityIcon');
      if (icon) {
        icon.textContent = rtt === null ? '📶' : rtt < 80 ? '📶' : rtt < 200 ? '📉' : '⚠️';
        icon.title = rtt !== null ? 'RTT: ' + Math.round(rtt) + 'ms' : 'Measuring…';
      }
    } catch(e) {}
  }, 3000);
}


// ── Keyboard Shortcuts ────────────────────────────────────────────────────────
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (!document.getElementById('controlBar').classList.contains('hidden')) {
    switch(e.key.toLowerCase()) {
      case 'm': toggleMic();         break;
      case 'v': toggleCamera();      break;
      case 's': toggleScreenShare(); break;
      case 'h': toggleHand();        break;
      case 'c': toggleChat();        break;
    }
  }
});


// ── Utilities ─────────────────────────────────────────────────────────────────
function copyRoomLink() {
  navigator.clipboard.writeText(window.location.href).then(() => {
    showToast('🔗 Room link copied!');
    const btn = document.getElementById('copyBtn');
    btn.textContent = '✅';
    setTimeout(() => { btn.textContent = '🔗'; }, 2000);
  });
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}

let _toastTimer = null;
function showToast(msg, duration) {
  duration = duration || 3000;
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.classList.remove('hidden');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => toast.classList.add('hidden'), duration);
}