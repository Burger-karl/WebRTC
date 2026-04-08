"""
server.py  -  MeetFree Production Signaling Server
"""

import logging
import os
import time
from collections import defaultdict

# flask_socketio newer versions removed the re-export of 'request'.
# Always import 'request' from flask directly for socket handlers.
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, join_room, emit, disconnect

from config import cfg
from auth import (
    create_token,
    validate_token_request,
    authenticate_socket,
    get_session,
    clear_session,
    require_auth,
)

logging.basicConfig(
    level=logging.DEBUG if cfg.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("meetfree")

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), '..', 'frontend'),
    static_folder=os.path.join(os.path.dirname(__file__), '..', 'frontend'),
)
app.config['SECRET_KEY'] = cfg.SECRET_KEY

# -- Socket.IO 
# message_queue=Redis URL  -> broadcasts across all Gunicorn workers via Redis
# message_queue=None       -> in-memory only (single process, fine for dev)
socketio = SocketIO(
    app,
    cors_allowed_origins=cfg.cors_origins,
    async_mode='eventlet',
    message_queue=cfg.socketio_message_queue,
    logger=cfg.DEBUG,
    engineio_logger=cfg.DEBUG,
)

# rooms[room_id] = { sid: { name, joined_at } }
rooms: dict = defaultdict(dict)

# Rate limiter buckets: { ip: [timestamps] }
_rate_buckets: dict = defaultdict(list)


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    _rate_buckets[ip] = [t for t in _rate_buckets[ip] if now - t < 60]
    if len(_rate_buckets[ip]) >= cfg.RATE_LIMIT_PER_MINUTE:
        return False
    _rate_buckets[ip].append(now)
    return True


# -- HTTP Routes

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/room/<room_id>')
def room(room_id):
    return render_template('room.html', room_id=room_id)


@app.route('/health')
def health():
    return jsonify({
        "status":       "ok",
        "active_rooms": len(rooms),
        "active_peers": sum(len(v) for v in rooms.values()),
        "redis":        bool(cfg.REDIS_URL),
    })


@app.route('/api/token', methods=['POST'])
def issue_token():
    """
    Issue a signed JWT for a user entering a room.

    Request body (JSON):  { "name": "Alice", "room": "my-room" }
    Response:             { "token": "...", "iceServers": [...], "expiresIn": 28800 }

    ICE servers (including TURN credentials) are returned here so they are
    never hard-coded in the frontend JS.
    """
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ip = ip.split(',')[0].strip()

    if not _check_rate_limit(ip):
        logger.warning(f"[RATE] Rate-limited IP={ip}")
        return jsonify({"error": "Too many requests. Please try again shortly."}), 429

    data    = request.get_json(silent=True) or {}
    name    = str(data.get("name", "")).strip()
    room_id = str(data.get("room", "")).strip()

    valid, error = validate_token_request(name, room_id)
    if not valid:
        return jsonify({"error": error}), 400

    if len(rooms.get(room_id, {})) >= cfg.MAX_USERS_PER_ROOM:
        return jsonify({"error": f"Room is full (max {cfg.MAX_USERS_PER_ROOM} participants)."}), 403

    token = create_token(name, room_id)
    logger.info(f"[TOKEN] Issued for name='{name}' room='{room_id}' ip={ip}")

    return jsonify({
        "token":      token,
        "iceServers": cfg.ice_servers(),
        "expiresIn":  cfg.JWT_EXPIRY_SECONDS,
    })


# -- Socket.IO: Connection lifecycle

@socketio.on('connect')
def handle_connect(auth):
    """
    Validate the JWT on every new socket connection.
    'request' here is Flask's request context (imported from flask, not flask_socketio).
    request.sid and request.environ are injected by Flask-SocketIO into
    Flask's request context during socket events.
    """
    allowed = authenticate_socket(request.sid, request.environ)
    if not allowed:
        return False  # returning False rejects the connection


@socketio.on('disconnect')
def handle_disconnect():
    sid     = request.sid
    session = get_session(sid)
    name    = session.get('sub', 'unknown') if session else 'unknown'

    for room_id, peers in list(rooms.items()):
        if sid in peers:
            del peers[sid]
            emit('peer_left', {'peerId': sid}, to=room_id)
            logger.info(f"[LEAVE] '{name}' left '{room_id}' | {len(peers)} remaining")
            if not peers:
                del rooms[room_id]
                logger.info(f"[ROOM] '{room_id}' is empty - removed")
            break

    clear_session(sid)


# -- Socket.IO: Signaling Events

@socketio.on('join')
@require_auth
def handle_join(data, _session):
    """
    Join a room. Room is taken from the JWT (not from the payload) so
    a user cannot spoof their way into a different room.
    """
    sid     = request.sid
    name    = _session['sub']
    room_id = _session['room']  # enforced from token - payload value ignored

    if len(rooms[room_id]) >= cfg.MAX_USERS_PER_ROOM:
        emit('error', {'code': 'ROOM_FULL', 'message': f'Room is full (max {cfg.MAX_USERS_PER_ROOM}).'})
        disconnect()
        return

    join_room(room_id)

    existing = [
        {'peerId': peer_sid, 'name': peer_info['name']}
        for peer_sid, peer_info in rooms[room_id].items()
    ]

    rooms[room_id][sid] = {'name': name, 'joined_at': time.time()}

    emit('room_peers', {'peers': existing})
    emit('peer_joined', {'peerId': sid, 'name': name}, to=room_id, skip_sid=sid)

    logger.info(f"[JOIN] '{name}' joined '{room_id}' | {len(rooms[room_id])} in room")


@socketio.on('offer')
@require_auth
def handle_offer(data, _session):
    sid     = request.sid
    room_id = _session['room']
    target  = data.get('target')

    if not target or target not in rooms.get(room_id, {}):
        logger.warning(f"[OFFER] Bad target '{target}' from sid={sid}")
        return

    emit('offer', {'sdp': data['sdp'], 'caller': sid}, to=target)
    logger.debug(f"[OFFER] {_session['sub']} -> {target}")


@socketio.on('answer')
@require_auth
def handle_answer(data, _session):
    sid     = request.sid
    room_id = _session['room']
    target  = data.get('target')

    if not target or target not in rooms.get(room_id, {}):
        logger.warning(f"[ANSWER] Bad target '{target}' from sid={sid}")
        return

    emit('answer', {'sdp': data['sdp'], 'answerer': sid}, to=target)
    logger.debug(f"[ANSWER] {_session['sub']} -> {target}")


@socketio.on('ice_candidate')
@require_auth
def handle_ice_candidate(data, _session):
    sid     = request.sid
    room_id = _session['room']
    target  = data.get('target')

    if not target or target not in rooms.get(room_id, {}):
        return  # silently drop - peer may have disconnected

    emit('ice_candidate', {'candidate': data.get('candidate'), 'sender': sid}, to=target)


@socketio.on('chat_message')
@require_auth
def handle_chat(data, _session):
    sid     = request.sid
    room_id = _session['room']
    name    = _session['sub']

    message = str(data.get('message', '')).strip()[:500]
    if not message:
        return

    emit('chat_message', {
        'message':  message,
        'sender':   name,
        'senderId': sid,
    }, to=room_id, skip_sid=sid)


@socketio.on('raise_hand')
@require_auth
def handle_raise_hand(data, _session):
    sid     = request.sid
    room_id = _session['room']
    raised  = bool(data.get('raised', True))

    emit('hand_raised', {
        'peerId': sid,
        'name':   _session['sub'],
        'raised': raised,
    }, to=room_id, skip_sid=sid)


@socketio.on('media_state')
@require_auth
def handle_media_state(data, _session):
    sid     = request.sid
    room_id = _session['room']

    emit('peer_media_state', {
        'peerId':  sid,
        'audioOn': bool(data.get('audioOn', True)),
        'videoOn': bool(data.get('videoOn', True)),
    }, to=room_id, skip_sid=sid)


# -- Entry point

if __name__ == '__main__':
    logger.info(f"Starting MeetFree on port {cfg.PORT}")
    logger.info(f"Redis:  {'enabled - ' + cfg.REDIS_URL if cfg.REDIS_URL else 'disabled (single-worker mode)'}")
    logger.info(f"TURN:   {'configured' if cfg.TURN_URL else 'not configured (STUN only)'}")
    logger.info(f"Debug:  {cfg.DEBUG}")
    socketio.run(app, host='0.0.0.0', port=cfg.PORT, debug=cfg.DEBUG)