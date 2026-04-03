/**
 * app.js -- WebRTC Client
 * CHAT FIXES:
 *   Bug 1 (duplicate message on sender): server now uses skip_sid so the
 *     sender's own message is not echoed back via socket.
 *   Bug 2 (message invisible to recipient): chat panel auto-opens on
 *     incoming message + a toast is always shown.
 */

// ---- State ---------------------------------------------------------------
let localStream     = null;
let screenStream    = null;
let localName       = 'Me';
let isMicOn         = true;
let isCamOn         = true;
let isScreenSharing = false;
let unreadChats     = 0;

const peerConnections = {};
const peerTiles       = {};
const socket          = io();

const ICE_CONFIG = {
  iceServers: [
    { urls: 'stun:stun.l.google.com:19302' },
    { urls: 'stun:stun1.l.google.com:19302' }
  ]
};

// ---- Pre-Call Preview ----------------------------------------------------
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
    console.warn('Preview camera unavailable:', err);
  }
}

function togglePreviewCamera() {
  if (!previewStream) return;
  previewCamEnabled = !previewCamEnabled;
  previewStream.getVideoTracks().forEach(t => (t.enabled = previewCamEnabled));
  document.getElementById('previewCamIcon').textContent = previewCamEnabled ? 'Cam On' : 'Cam Off';
  document.getElementById('previewVideo').style.display = previewCamEnabled ? 'block' : 'none';
  document.getElementById('previewPlaceholder').style.display = previewCamEnabled ? 'none' : 'flex';
}

function togglePreviewMic() {
  if (!previewStream) return;
  previewMicEnabled = !previewMicEnabled;
  previewStream.getAudioTracks().forEach(t => (t.enabled = previewMicEnabled));
  document.getElementById('previewMicIcon').textContent = previewMicEnabled ? 'Mic On' : 'Mic Off';
}

startPreview();

// ---- Join Room -----------------------------------------------------------
async function enterRoom() {
  localName = (document.getElementById('nameInput').value.trim()) || 'Guest';

  if (previewStream) previewStream.getTracks().forEach(t => t.stop());

  try {
    localStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
  } catch (err) {
    showToast('Could not access camera/mic.');
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ video: false, audio: true });
      isCamOn = false;
    } catch (e) {
      showToast('No media devices available.');
      return;
    }
  }

  document.getElementById('preCallModal').classList.add('hidden');
  document.getElementById('roomContainer').classList.remove('hidden');
  document.getElementById('controlBar').classList.remove('hidden');

  addLocalTile();
  socket.emit('join', { room: ROOM_ID, name: localName });
  updateControlBarState();
}

// ---- Video Grid ----------------------------------------------------------
function addLocalTile() {
  document.getElementById('videoGrid').appendChild(
    createTile('local', localName + ' (You)', localStream, true)
  );
  updateGridLayout();
}

function createTile(id, label, stream, muted) {
  const container       = document.createElement('div');
  container.className   = 'video-tile';
  container.id          = 'tile-' + id;

  const video           = document.createElement('video');
  video.autoplay        = true;
  video.playsInline     = true;
  video.muted           = !!muted;
  if (stream) video.srcObject = stream;

  const placeholder     = document.createElement('div');
  placeholder.className = 'video-placeholder';
  placeholder.id        = 'placeholder-' + id;
  placeholder.innerHTML = '<span>person</span><p>' + label + '</p>';

  const nameTag         = document.createElement('div');
  nameTag.className     = 'name-tag';
  nameTag.textContent   = label;

  container.appendChild(video);
  container.appendChild(placeholder);
  container.appendChild(nameTag);

  if (!stream || !stream.getVideoTracks().length) {
    video.style.display       = 'none';
    placeholder.style.display = 'flex';
  }

  peerTiles[id] = { videoEl: video, container, placeholder };
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
  updateGridLayout();
}

// ---- Peer Connections ----------------------------------------------------
function createPeerConnection(peerId, isInitiator) {
  const pc = new RTCPeerConnection(ICE_CONFIG);
  peerConnections[peerId] = pc;

  if (localStream) localStream.getTracks().forEach(t => pc.addTrack(t, localStream));

  pc.ontrack = (event) => {
    const [remoteStream] = event.streams;
    if (peerTiles[peerId]) {
      peerTiles[peerId].videoEl.srcObject = remoteStream;
      if (event.track.kind === 'video') {
        peerTiles[peerId].videoEl.style.display     = 'block';
        peerTiles[peerId].placeholder.style.display = 'none';
      }
    }
  };

  pc.onicecandidate = (event) => {
    if (event.candidate)
      socket.emit('ice_candidate', { target: peerId, candidate: event.candidate });
  };

  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'disconnected' || pc.connectionState === 'failed')
      handlePeerDisconnect(peerId);
    if (pc.connectionState === 'connected')
      showToast('Connected to a participant');
  };

  if (isInitiator) {
    pc.createOffer()
      .then(o => pc.setLocalDescription(o))
      .then(() => socket.emit('offer', { target: peerId, sdp: pc.localDescription }))
      .catch(e => console.error('createOffer error:', e));
  }
  return pc;
}

function handlePeerDisconnect(peerId) {
  if (peerConnections[peerId]) { peerConnections[peerId].close(); delete peerConnections[peerId]; }
  removeTile(peerId);
  showToast('A participant left the room');
}

// ---- Signaling Events ----------------------------------------------------
socket.on('room_peers', ({ peers }) => {
  peers.forEach(peerId => {
    document.getElementById('videoGrid').appendChild(createTile(peerId, 'Participant', null, false));
    updateGridLayout();
    createPeerConnection(peerId, true);
  });
});

socket.on('peer_joined', ({ peerId }) => {
  showToast('Someone joined the room');
  document.getElementById('videoGrid').appendChild(createTile(peerId, 'Participant', null, false));
  updateGridLayout();
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
    catch(e) { console.warn('ICE error:', e); }
  }
});

socket.on('peer_left', ({ peerId }) => handlePeerDisconnect(peerId));

// ---- CHAT (fixed) --------------------------------------------------------
/**
 * This handler only fires for messages from OTHER people.
 * The server uses skip_sid=request.sid so we never receive our own
 * messages back -- that was causing the duplicate bubble (Bug 1).
 *
 * Bug 2 fix: we now auto-open the chat panel AND show a toast so
 * the recipient can never miss an incoming message.
 */
socket.on('chat_message', ({ message, sender }) => {
  // Always write to the chat log
  appendChatMessage(sender, message, false);

  const panel = document.getElementById('chatPanel');

  // Auto-open the panel so the message is immediately visible
  if (panel.classList.contains('hidden')) {
    panel.classList.remove('hidden');
    document.getElementById('chatInput').focus();
    unreadChats = 0;
    document.getElementById('chatBadge').classList.add('hidden');
  }

  // Show a toast so the user notices even if looking elsewhere
  const preview = message.length > 45 ? message.slice(0, 45) + '...' : message;
  showToast(sender + ': ' + preview, 4000);
});

// ---- Controls ------------------------------------------------------------
function toggleMic() {
  isMicOn = !isMicOn;
  if (localStream) localStream.getAudioTracks().forEach(t => (t.enabled = isMicOn));
  updateControlBarState();
}

function toggleCamera() {
  isCamOn = !isCamOn;
  if (localStream) localStream.getVideoTracks().forEach(t => (t.enabled = isCamOn));
  if (peerTiles['local']) {
    peerTiles['local'].videoEl.style.display     = isCamOn ? 'block' : 'none';
    peerTiles['local'].placeholder.style.display = isCamOn ? 'none'  : 'flex';
  }
  updateControlBarState();
}

async function toggleScreenShare() {
  if (!isScreenSharing) {
    try {
      screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true });
      const screenTrack = screenStream.getVideoTracks()[0];
      Object.values(peerConnections).forEach(pc => {
        const sender = pc.getSenders().find(s => s.track && s.track.kind === 'video');
        if (sender) sender.replaceTrack(screenTrack);
      });
      if (peerTiles['local']) peerTiles['local'].videoEl.srcObject = screenStream;
      isScreenSharing = true;
      showToast('Screen sharing started');
      screenTrack.onended = stopScreenShare;
    } catch(e) { console.warn('Screen share cancelled:', e); }
  } else { stopScreenShare(); }
}

function stopScreenShare() {
  if (!screenStream) return;
  screenStream.getTracks().forEach(t => t.stop());
  isScreenSharing = false;
  const camTrack = localStream && localStream.getVideoTracks()[0];
  if (camTrack) {
    Object.values(peerConnections).forEach(pc => {
      const sender = pc.getSenders().find(s => s.track && s.track.kind === 'video');
      if (sender) sender.replaceTrack(camTrack);
    });
    if (peerTiles['local']) peerTiles['local'].videoEl.srcObject = localStream;
  }
  showToast('Screen sharing stopped');
}

function leaveCall() {
  Object.values(peerConnections).forEach(pc => pc.close());
  if (localStream) localStream.getTracks().forEach(t => t.stop());
  socket.disconnect();
  window.location.href = '/';
}

function updateControlBarState() {
  document.getElementById('micIcon').textContent = isMicOn ? 'Mic' : 'Muted';
  document.getElementById('micBtn').className = 'ctrl-btn ' + (isMicOn ? 'active' : 'muted');
  document.getElementById('camIcon').textContent = isCamOn ? 'Cam' : 'Cam Off';
  document.getElementById('camBtn').className = 'ctrl-btn ' + (isCamOn ? 'active' : 'muted');
}

// ---- Chat UI -------------------------------------------------------------
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
  appendChatMessage('You', msg, true);   // show locally immediately
  input.value = '';
}

function appendChatMessage(sender, message, isSelf) {
  const container = document.getElementById('chatMessages');
  const el        = document.createElement('div');
  el.className    = 'chat-msg ' + (isSelf ? 'self' : 'other');
  el.innerHTML    = '<span class="chat-sender">' + escapeHtml(sender) + '</span>' +
                    '<span class="chat-text">'   + escapeHtml(message) + '</span>';
  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
}

function escapeHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ---- Utilities -----------------------------------------------------------
function copyRoomLink() {
  navigator.clipboard.writeText(window.location.href).then(() => {
    showToast('Room link copied!');
  });
}

let toastTimer = null;
function showToast(msg, duration) {
  duration = duration || 3000;
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add('hidden'), duration);
}