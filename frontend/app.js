// ── Bootstrap: read token + ICE config from sessionStorage ───────────────────
const _token      = sessionStorage.getItem('meetfree_token');
const _iceRaw     = sessionStorage.getItem('meetfree_ice');
const _savedName  = sessionStorage.getItem('meetfree_name');

if (!_token) {
  // No token → redirect to landing page with the room pre-filled
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
let unreadChats     = 0;

const peerConnections = {};   // peerId → RTCPeerConnection
const peerTiles       = {};   // peerId → { videoEl, placeholder, nameTag, muteIcon, camOffIcon, handIcon }
const peerNames       = {};   // peerId → display name

// ── Socket.IO — pass JWT as query param ───────────────────────────────────────
// The server's authenticate_socket() reads it from QUERY_STRING.
const socket = io({
  query:              { token: _token },
  reconnectionDelay:  1000,
  reconnectionAttempts: 5,
});

socket.on('connect_error', (err) => {
  console.error('[SOCKET] Connect error:', err.message);
  if (err.message.includes('auth') || err.message.includes('401')) {
    sessionStorage.clear();
    window.location.href = '/?room=' + encodeURIComponent(ROOM_ID) + '&error=session_expired';
  }
});

// ── Auth guard UI ─────────────────────────────────────────────────────────────
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
  } catch (err) {
    console.warn('[PREVIEW] Camera unavailable:', err.name);
  }
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
    isCamOn = true; isMicOn = true;
  } catch (err) {
    console.warn('[MEDIA] Video+audio failed, trying audio only:', err.name);
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ video: false, audio: true });
      isCamOn = false;
    } catch (e) {
      showToast('Could not access any media devices. Check permissions.');
      return;
    }
  }

  // Apply pre-call toggle states
  if (!previewCamEnabled) { localStream.getVideoTracks().forEach(t => (t.enabled = false)); isCamOn = false; }
  if (!previewMicEnabled) { localStream.getAudioTracks().forEach(t => (t.enabled = false)); isMicOn = false; }

  document.getElementById('preCallModal').classList.add('hidden');
  document.getElementById('roomContainer').classList.remove('hidden');
  document.getElementById('controlBar').classList.remove('hidden');

  addLocalTile();
  updateControlBarState();

  // Join the signaling room — server enforces the room from the JWT
  socket.emit('join', { room: ROOM_ID, name: localName });

  // Start quality monitoring
  startQualityMonitor();
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
  const container = document.createElement('div');
  container.className = 'video-tile';
  container.id = 'tile-' + id;

  const video = document.createElement('video');
  video.autoplay = true; video.playsInline = true; video.muted = !!muted;
  if (stream) video.srcObject = stream;

  const placeholder = document.createElement('div');
  placeholder.className = 'video-placeholder';
  placeholder.id = 'placeholder-' + id;
  placeholder.innerHTML = '<span>👤</span><p>' + escapeHtml(label) + '</p>';

  const nameTag = document.createElement('div');
  nameTag.className = 'name-tag';
  nameTag.id = 'nametag-' + id;
  nameTag.textContent = label;

  // Mute indicator overlay (top-right of tile)
  const muteIcon = document.createElement('div');
  muteIcon.className = 'tile-overlay-icon mute-icon hidden';
  muteIcon.textContent = '🔇';
  muteIcon.id = 'mute-' + id;

  // Camera-off indicator
  const camOffIcon = document.createElement('div');
  camOffIcon.className = 'tile-overlay-icon camoff-icon hidden';
  camOffIcon.textContent = '🚫';
  camOffIcon.id = 'camoff-' + id;

  // Hand raised indicator
  const handIcon = document.createElement('div');
  handIcon.className = 'tile-overlay-icon hand-icon hidden';
  handIcon.textContent = '✋';
  handIcon.id = 'hand-' + id;

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
  const count = document.getElementById('videoGrid').children.length;
  const el = document.getElementById('participantNum');
  if (el) el.textContent = count;
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

  pc.onicecandidate = (event) => {
    if (event.candidate)
      socket.emit('ice_candidate', { target: peerId, candidate: event.candidate });
  };

  pc.onconnectionstatechange = () => {
    const state = pc.connectionState;
    console.log(`[CONN] ${peerId} → ${state}`);
    if (state === 'disconnected' || state === 'failed') handlePeerDisconnect(peerId);
    if (state === 'connected') showToast('✅ Connected to ' + (peerNames[peerId] || 'a participant'));
  };

  if (isInitiator) {
    pc.createOffer()
      .then(o => pc.setLocalDescription(o))
      .then(() => socket.emit('offer', { target: peerId, sdp: pc.localDescription }))
      .catch(e => console.error('[OFFER] Error:', e));
  }
  return pc;
}

function handlePeerDisconnect(peerId) {
  if (peerConnections[peerId]) { peerConnections[peerId].close(); delete peerConnections[peerId]; }
  removeTile(peerId);
  showToast('👋 ' + (peerNames[peerId] || 'A participant') + ' left');
}


// ── Signaling Events ──────────────────────────────────────────────────────────
socket.on('room_peers', ({ peers }) => {
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
    try { await pc.addIceCandidate(new RTCIceCandidate(candidate)); }
    catch(e) { /* ICE candidates can arrive after renegotiation — not fatal */ }
  }
});

socket.on('peer_left', ({ peerId }) => handlePeerDisconnect(peerId));

// ── Chat ──────────────────────────────────────────────────────────────────────
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

// ── Hand Raise ────────────────────────────────────────────────────────────────
socket.on('hand_raised', ({ peerId, name, raised }) => {
  const tile = peerTiles[peerId];
  if (tile) {
    tile.handIcon.classList.toggle('hidden', !raised);
  }
  if (raised) showToast('✋ ' + name + ' raised their hand', 4000);
});

// ── Remote media state (mute/cam-off indicators on remote tiles) ──────────────
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
    if (tile.videoEl.srcObject) {
      tile.videoEl.style.display     = 'block';
      tile.placeholder.style.display = 'none';
    }
  }
});


// ── Controls ──────────────────────────────────────────────────────────────────

function toggleMic() {
  isMicOn = !isMicOn;
  if (localStream) localStream.getAudioTracks().forEach(t => (t.enabled = isMicOn));
  // Show mute icon on our own tile
  if (peerTiles['local']) peerTiles['local'].muteIcon.classList.toggle('hidden', isMicOn);
  // Broadcast state to remote peers
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
      screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
      const screenTrack = screenStream.getVideoTracks()[0];
      Object.values(peerConnections).forEach(pc => {
        const s = pc.getSenders().find(s => s.track && s.track.kind === 'video');
        if (s) s.replaceTrack(screenTrack);
      });
      if (peerTiles['local']) peerTiles['local'].videoEl.srcObject = screenStream;
      isScreenSharing = true;
      document.getElementById('screenIcon').textContent = '⏹️';
      document.getElementById('screenBtn').classList.add('active');
      showToast('🖥️ Screen sharing started');
      screenTrack.onended = stopScreenShare;
    } catch(e) { console.warn('[SCREEN] Cancelled or denied:', e.name); }
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
  document.getElementById('handIcon').textContent = isHandRaised ? '✋' : '✋';
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
  const micBtn  = document.getElementById('micBtn');
  const camBtn  = document.getElementById('camBtn');
  document.getElementById('micIcon').textContent = isMicOn ? '🎙️' : '🔇';
  micBtn.className = 'ctrl-btn ' + (isMicOn ? 'active' : 'muted');
  document.getElementById('camIcon').textContent = isCamOn ? '📷' : '🚫';
  camBtn.className = 'ctrl-btn ' + (isCamOn ? 'active' : 'muted');
}


// ── Chat UI ───────────────────────────────────────────────────────────────────
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

  const ts = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  el.innerHTML =
    '<span class="chat-sender">' + escapeHtml(sender) + '</span>' +
    '<span class="chat-text">'   + escapeHtml(message) + '</span>' +
    '<span class="chat-ts">'     + ts + '</span>';
  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
}


// ── Connection Quality Monitor ────────────────────────────────────────────────
let qualityInterval = null;

function startQualityMonitor() {
  qualityInterval = setInterval(async () => {
    const pcs = Object.values(peerConnections);
    if (!pcs.length) return;

    // Sample the first active connection
    const pc = pcs[0];
    if (pc.connectionState !== 'connected') return;

    try {
      const stats = await pc.getStats();
      let rtt = null;
      stats.forEach(report => {
        if (report.type === 'candidate-pair' && report.state === 'succeeded') {
          if (report.currentRoundTripTime !== undefined) {
            rtt = report.currentRoundTripTime * 1000; // convert to ms
          }
        }
      });

      const icon = document.getElementById('qualityIcon');
      if (icon) {
        if      (rtt === null)  icon.textContent = '📶';
        else if (rtt < 80)      icon.textContent = '📶'; // good
        else if (rtt < 200)     icon.textContent = '📉'; // fair
        else                    icon.textContent = '⚠️'; // poor
        icon.title = rtt !== null ? `RTT: ${Math.round(rtt)}ms` : 'Measuring…';
      }
    } catch(e) { /* stats not yet available */ }
  }, 3000);
}


// ── Keyboard Shortcuts ────────────────────────────────────────────────────────
document.addEventListener('keydown', (e) => {
  // Don't fire when typing in an input
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