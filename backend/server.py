"""
WebRTC Signaling Server
-----------------------
Uses Flask + Flask-SocketIO to relay SDP offers/answers and ICE candidates
between peers in the same room. No video data passes through this server —
it only brokers the initial "handshake" so browsers can connect peer-to-peer.
"""

from flask import Flask, render_template, send_from_directory
from flask_socketio import SocketIO, join_room, leave_room, emit
import os

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), '..', 'frontend'),
    static_folder=os.path.join(os.path.dirname(__file__), '..', 'frontend')
)
app.config['SECRET_KEY'] = 'webrtc-secret-key-change-in-production'

# eventlet async mode gives best performance for many concurrent connections
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Track who is in each room: { room_id: [socket_id, ...] }
rooms = {}


# ─── HTTP Routes ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    """Landing page — enter a room name to start or join a meeting."""
    return render_template('index.html')


@app.route('/room/<room_id>')
def room(room_id):
    """The actual video call page."""
    return render_template('room.html', room_id=room_id)


# ─── Socket.IO Events ────────────────────────────────────────────────────────

@socketio.on('join')
def handle_join(data):
    """
    A peer joins a room. We notify existing peers so they can initiate
    the WebRTC offer/answer handshake with the new arrival.
    """
    from flask_socketio import request
    room_id = data['room']
    join_room(room_id)

    if room_id not in rooms:
        rooms[room_id] = []

    # Tell the new peer who else is already in the room
    existing_peers = rooms[room_id].copy()
    emit('room_peers', {'peers': existing_peers})

    # Tell everyone else that a new peer arrived
    emit('peer_joined', {'peerId': request.sid}, to=room_id, skip_sid=request.sid)

    rooms[room_id].append(request.sid)
    print(f"[JOIN] {request.sid} joined room '{room_id}' | peers now: {rooms[room_id]}")


@socketio.on('offer')
def handle_offer(data):
    """
    Relay an SDP offer from the caller to a specific peer.
    The offer contains the caller's media capabilities.
    """
    from flask_socketio import request
    target = data['target']
    emit('offer', {
        'sdp': data['sdp'],
        'caller': request.sid
    }, to=target)
    print(f"[OFFER] {request.sid} → {target}")


@socketio.on('answer')
def handle_answer(data):
    """
    Relay an SDP answer back to the caller.
    The answer confirms which codecs/formats will be used.
    """
    from flask_socketio import request
    target = data['target']
    emit('answer', {
        'sdp': data['sdp'],
        'answerer': request.sid
    }, to=target)
    print(f"[ANSWER] {request.sid} → {target}")


@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    """
    Relay an ICE candidate to a peer.
    ICE candidates are the possible network routes to reach this peer.
    Once exchanged, browsers pick the best route and connect directly.
    """
    from flask_socketio import request
    target = data['target']
    emit('ice_candidate', {
        'candidate': data['candidate'],
        'sender': request.sid
    }, to=target)


@socketio.on('disconnect')
def handle_disconnect():
    """
    Clean up when a peer disconnects. Notify remaining room members.
    """
    from flask_socketio import request
    sid = request.sid
    for room_id, peers in rooms.items():
        if sid in peers:
            peers.remove(sid)
            emit('peer_left', {'peerId': sid}, to=room_id)
            print(f"[LEAVE] {sid} left room '{room_id}' | peers now: {peers}")
            break


@socketio.on('chat_message')
def handle_chat(data):
    """Relay in-room chat messages to all OTHER participants (skip sender)."""
    from flask_socketio import request
    room_id = data['room']
    # skip_sid=request.sid prevents the sender receiving their own message
    # back as an 'other' bubble (they already appended it locally as 'You')
    emit('chat_message', {
        'message': data['message'],
        'sender': data.get('name', 'Anonymous'),
        'senderId': request.sid
    }, to=room_id, skip_sid=request.sid)


if __name__ == '__main__':
    print("🚀 WebRTC Signaling Server running on http://localhost:5000")
    print("   Open two browser tabs to the same room URL to test a call.")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)