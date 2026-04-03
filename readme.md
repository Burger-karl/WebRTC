# MeetFree — Self-Hosted WebRTC Video Conferencing

A Whereby-inspired, unlimited video conferencing app built with:
- **Python + Flask-SocketIO** for the WebRTC signaling server
- **aiortc** for server-side WebRTC (available for future media relay features)
- **Vanilla JS WebRTC API** + **Socket.IO** for the browser client
- No time limits. No third-party APIs. Fully self-hosted.

---

## Project Structure

```
webrtc-app/
├── backend/
│   ├── server.py          ← Flask-SocketIO signaling server
│   └── requirements.txt
└── frontend/
    ├── index.html         ← Landing page (enter room name)
    ├── room.html          ← Video call room UI
    ├── app.js             ← WebRTC + signaling client logic
    └── style.css          ← Whereby-inspired dark UI
```

---

## Setup & Run

### 1. Install dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 2. Start the server

```bash
python server.py
```

You should see:
```
🚀 WebRTC Signaling Server running on http://localhost:5000
```

### 3. Test a call

Open **two browser tabs** (or two different browsers) and go to:
```
http://localhost:5000/room/my-test-room
```

Both tabs will be connected to the same room. Enter your name in each and click "Join Meeting".

---

## How It Works

### The Signaling Flow

```
Browser A                 Signaling Server (Python)       Browser B
    |                            |                             |
    |──── socket: join room ────>|                             |
    |                            |<──── socket: join room ─────|
    |<── room_peers: [B's ID] ───|                             |
    |                            |────── peer_joined ─────────>|
    |                            |                             |
    |── createOffer() ──────────>|                             |
    |── socket: offer ──────────>|──── socket: offer ─────────>|
    |                            |          setRemoteDescription|
    |                            |          createAnswer()      |
    |                            |<──── socket: answer ────────|
    |<── socket: answer ─────────|                             |
    | setRemoteDescription()     |                             |
    |                            |                             |
    |── ICE candidate ──────────>|──── ICE candidate ─────────>|
    |<── ICE candidate ──────────|<──── ICE candidate ─────────|
    |                            |                             |
    |<══════════ Direct P2P video/audio stream ═══════════════>|
    |         (Signaling server no longer involved)            |
```

### Key Concepts

- **SDP (Session Description Protocol)**: Each peer describes what audio/video formats it supports. The offer/answer exchange picks a common format.
- **ICE Candidates**: Network addresses (local IP, public IP via STUN) that peers can use to reach each other. The browser picks the best available path.
- **STUN Server**: A public server (Google's free one is used here) that tells your browser its public IP address so peers behind different NATs can find each other.
- **Signaling Server**: The Python server only exchanges the above metadata. Once connected, all video/audio flows P2P — the server is not involved.

---

## Features

- ✅ Unlimited meeting time (self-hosted, no third-party limits)
- ✅ No logins required for participants
- ✅ Pre-call camera/mic preview
- ✅ Real-time video grid (auto-layout: 1, 2, 4, many participants)
- ✅ Mute/unmute microphone
- ✅ Camera on/off
- ✅ Screen sharing (with automatic camera restore on stop)
- ✅ In-room text chat
- ✅ One-click room link copy
- ✅ Whereby-inspired clean dark UI
- ✅ Fully responsive (mobile-friendly)

---

## Deploying to a Server (Production)

To allow calls between users on different networks:

1. **HTTPS is required** — browsers only allow camera access on HTTPS.
   Use Let's Encrypt + Nginx as a reverse proxy.

2. **Your STUN server config** (in `app.js`) already uses Google's free public STUN:
   ```js
   { urls: 'stun:stun.l.google.com:19302' }
   ```
   This works for most home/office networks.

3. **For strict corporate firewalls**, add a TURN server (TURN relays media when P2P fails):
   ```bash
   # Install coturn (open source TURN server)
   sudo apt install coturn
   ```
   Then add to `ICE_CONFIG` in `app.js`:
   ```js
   { urls: 'turn:your-server.com:3478', username: 'user', credential: 'pass' }
   ```

4. **Run with gunicorn** in production:
   ```bash
   gunicorn --worker-class eventlet -w 1 server:app
   ```

---

## Environment Variables (optional)

Create a `.env` file in `backend/`:
```
SECRET_KEY=your-random-secret-here
PORT=5000
```