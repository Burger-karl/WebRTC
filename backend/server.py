"""
server.py — MeetFree Signaling Server (feature-dev branch)

New features in this version:
  1. Persistent Chat (MySQL)
       - Every chat message is saved to MySQL via database.save_message()
       - When a user joins, the last 50 messages are sent via 'chat_history'
       - Messages survive page refresh and reconnection

  2. Room Passwords
       - POST /api/check-room  — tells the client if a room needs a password
       - POST /api/token       — now accepts 'password' field and validates it
       - Password is bcrypt-hashed in MySQL via database.create_room()
       - First joiner sets the password (can be blank = open room)

  3. Host Controls — Remote Mute
       - Socket event 'mute_participant': host can force-mute any participant
       - Socket event 'unmute_participant': host can remove the mute
       - Muted participant receives 'you_were_muted' and their mic is disabled
       - Mute state is persisted to MySQL for late joiners
       - Kick (remove_participant) was already implemented — kept here
"""

import logging
import os
import time

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
from payments import payments_bp, is_subscribed
from bookings import bookings_bp
import database as db

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if cfg.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("meetfree")

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), '..', 'frontend'),
    static_folder=os.path.join(os.path.dirname(__file__), '..', 'frontend'),
)
app.config['SECRET_KEY'] = cfg.SECRET_KEY
app.register_blueprint(payments_bp)
app.register_blueprint(bookings_bp)

# ── Socket.IO ─────────────────────────────────────────────────────────────────
socketio = SocketIO(
    app,
    cors_allowed_origins=cfg.cors_origins,
    async_mode='eventlet',
    message_queue=cfg.socketio_message_queue,
    logger=cfg.DEBUG,
    engineio_logger=cfg.DEBUG,
)

# ── In-memory state ───────────────────────────────────────────────────────────
rooms:       dict = {}   # room_id → { sid: { name, joined_at, is_admin } }
lobby:       dict = {}   # room_id → { sid: { name, requested_at } }
room_admins: dict = {}   # room_id → sid
_rate_buckets: dict = {}


def _check_rate_limit(ip: str) -> bool:
    now    = time.time()
    bucket = [t for t in _rate_buckets.get(ip, []) if now - t < 60]
    if len(bucket) >= cfg.RATE_LIMIT_PER_MINUTE:
        _rate_buckets[ip] = bucket
        return False
    bucket.append(now)
    _rate_buckets[ip] = bucket
    return True


# ── Initialise MySQL on startup ───────────────────────────────────────────────
with app.app_context():
    db.init_db(cfg)


# ── HTTP Routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/room/<room_id>')
def room(room_id):
    return render_template('room.html', room_id=room_id)


@app.route('/health')
def health():
    return jsonify({
        "status":        "ok",
        "active_rooms":  len(rooms),
        "active_peers":  sum(len(v) for v in rooms.values()),
        "lobby_waiting": sum(len(v) for v in lobby.values()),
        "redis":         bool(cfg.REDIS_URL),
        "mysql":         db._db_available,
    })


@app.route('/api/check-room', methods=['POST'])
def check_room():
    """
    FEATURE 2 — Room Passwords.
    Called by the landing page before issuing a token so the UI can
    show a password field if the room is password-protected.

    Request:  { "room": "my-room" }
    Response: { "exists": true, "hasPassword": true }

    The client uses this to conditionally show the password input.
    """
    data    = request.get_json(silent=True) or {}
    room_id = str(data.get("room", "")).strip()

    if not room_id:
        return jsonify({"error": "room is required"}), 400

    # Check if room is live (someone is in it right now)
    room_is_live = bool(rooms.get(room_id))

    # Check if room has a DB record with a password
    has_password = False
    if room_is_live and db._db_available:
        room_record = db.get_room(cfg, room_id)
        has_password = bool(room_record and room_record.get('password_hash'))

    return jsonify({
        "exists":      room_is_live,
        "hasPassword": has_password,
    })


@app.route('/api/token', methods=['POST'])
def issue_token():
    """
    Issue a signed JWT.
    FEATURE 2: Now validates room password if one is set.
    """
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ip = ip.split(',')[0].strip()

    if not _check_rate_limit(ip):
        return jsonify({"error": "Too many requests. Please try again."}), 429

    data     = request.get_json(silent=True) or {}
    name     = str(data.get("name", "")).strip()
    room_id  = str(data.get("room", "")).strip()
    email    = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", "")).strip()

    valid, error = validate_token_request(name, room_id)
    if not valid:
        return jsonify({"error": error}), 400

    if len(rooms.get(room_id, {})) >= cfg.MAX_USERS_PER_ROOM:
        return jsonify({"error": f"Room is full (max {cfg.MAX_USERS_PER_ROOM})."}), 403

    # ── FEATURE 2: Password check ─────────────────────────────
    # Only check password if the room is currently live (someone is in it).
    # If the room is empty, the joiner becomes host and can set a password.
    if rooms.get(room_id):
        if not db.verify_room_password(cfg, room_id, password):
            return jsonify({"error": "Incorrect room password."}), 403

    # ── Stripe subscription check ─────────────────────────────
    if cfg.STRIPE_SECRET_KEY and email:
        allowed, reason = is_subscribed(email)
        if not allowed:
            return jsonify({
                "error":      "Your subscription has expired.",
                "reason":     reason,
                "redirectTo": "/pricing",
            }), 402

    token = create_token(name, room_id)
    logger.info(f"[TOKEN] Issued: name='{name}' room='{room_id}' ip={ip}")

    return jsonify({
        "token":      token,
        "iceServers": cfg.ice_servers(),
        "expiresIn":  cfg.JWT_EXPIRY_SECONDS,
    })


# ── Socket.IO: Connection lifecycle ──────────────────────────────────────────

@socketio.on('connect')
def handle_connect(auth):
    allowed = authenticate_socket(request.sid, request.environ)
    if not allowed:
        return False


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

            if room_admins.get(room_id) == sid:
                del room_admins[room_id]
                if peers:
                    new_admin_sid = next(iter(peers))
                    room_admins[room_id] = new_admin_sid
                    emit('you_are_admin', {}, to=new_admin_sid)
                    emit('admin_changed', {'newAdminId': new_admin_sid}, to=room_id)
                    logger.info(f"[HOST] New host: {peers[new_admin_sid]['name']} in '{room_id}'")

            if not peers:
                del rooms[room_id]
                lobby.pop(room_id, None)
                room_admins.pop(room_id, None)
                # FEATURE 1: Clean up chat history when room is permanently closed
                # Comment out the next line if you want to keep history forever
                db.delete_room_history(cfg, room_id)
                # FEATURE 2: Remove room password record when room closes
                db.delete_room(cfg, room_id)
            break

    for room_id, waiters in list(lobby.items()):
        if sid in waiters:
            del waiters[sid]
            if not waiters:
                lobby.pop(room_id, None)
            break

    clear_session(sid)


# ── Socket.IO: Lobby ──────────────────────────────────────────────────────────

@socketio.on('request_join')
@require_auth
def handle_request_join(data, _session):
    """
    User requests entry to a room.
    FEATURE 2: First joiner also sends optional room password to set.
    FEATURE 1: On admission, room chat history is sent to the new user.
    """
    sid      = request.sid
    name     = _session['sub']
    room_id  = _session['room']
    password = str(data.get("password", "")).strip()

    if sid in rooms.get(room_id, {}):
        logger.warning(f"[JOIN] Duplicate request_join from '{name}' — ignoring")
        return

    room_is_empty = not rooms.get(room_id)

    if room_is_empty:
        # ── First joiner: create room, become host ────────────
        rooms[room_id]       = {}
        room_admins[room_id] = sid

        # FEATURE 2: Register room with optional password in DB
        db.create_room(cfg, room_id, created_by=name, password=password or None)

        _admit_to_room(sid, name, room_id)
        emit('you_are_admin', {})
        logger.info(f"[HOST] '{name}' created room '{room_id}' | password={'set' if password else 'none'}")
    else:
        # ── Room exists: send to lobby ────────────────────────
        if room_id not in lobby:
            lobby[room_id] = {}
        lobby[room_id][sid] = {'name': name, 'requested_at': time.time()}
        join_room(f"lobby_{room_id}")
        emit('waiting_for_approval', {'message': 'Please wait — the host will let you in shortly.'})
        admin_sid = room_admins.get(room_id)
        if admin_sid:
            emit('lobby_request', {'peerId': sid, 'name': name}, to=admin_sid)
        logger.info(f"[LOBBY] '{name}' waiting to join '{room_id}'")


@socketio.on('admit_user')
@require_auth
def handle_admit_user(data, _session):
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')

    if room_admins.get(room_id) != sid:
        emit('error', {'code': 'NOT_ADMIN', 'message': 'Only the host can admit users.'})
        return

    waiter = lobby.get(room_id, {}).get(target_sid)
    if not waiter:
        emit('error', {'code': 'NOT_IN_LOBBY', 'message': 'User is no longer in the lobby.'})
        return

    del lobby[room_id][target_sid]
    if not lobby[room_id]:
        del lobby[room_id]

    emit('admission_result', {'admitted': True, 'message': 'You have been admitted.'}, to=target_sid)
    _admit_to_room(target_sid, waiter['name'], room_id)
    logger.info(f"[LOBBY] '{_session['sub']}' admitted '{waiter['name']}' to '{room_id}'")


@socketio.on('deny_user')
@require_auth
def handle_deny_user(data, _session):
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')

    if room_admins.get(room_id) != sid:
        emit('error', {'code': 'NOT_ADMIN', 'message': 'Only the host can deny users.'})
        return

    room_lobby = lobby.get(room_id, {})
    if target_sid in room_lobby:
        waiter_name = room_lobby.pop(target_sid)['name']
        if not room_lobby:
            lobby.pop(room_id, None)
        emit('admission_result', {'admitted': False, 'message': 'The host denied your request.'}, to=target_sid)
        logger.info(f"[LOBBY] '{_session['sub']}' denied '{waiter_name}'")


@socketio.on('remove_participant')
@require_auth
def handle_remove_participant(data, _session):
    """FEATURE 3: Host kicks a participant."""
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')

    if room_admins.get(room_id) != sid:
        emit('error', {'code': 'NOT_ADMIN', 'message': 'Only the host can remove participants.'})
        return

    if target_sid in rooms.get(room_id, {}):
        target_name = rooms[room_id][target_sid]['name']
        emit('you_were_removed', {'message': 'You have been removed by the host.'}, to=target_sid)
        logger.info(f"[HOST] '{_session['sub']}' removed '{target_name}' from '{room_id}'")


# ── FEATURE 3: Host Controls — Remote Mute ────────────────────────────────────

@socketio.on('mute_participant')
@require_auth
def handle_mute_participant(data, _session):
    """
    Host force-mutes a participant.
    The target receives 'you_were_muted' and their client disables the mic.
    Mute state is persisted to MySQL for late joiners.
    """
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')

    if room_admins.get(room_id) != sid:
        emit('error', {'code': 'NOT_ADMIN', 'message': 'Only the host can mute participants.'})
        return

    if target_sid not in rooms.get(room_id, {}):
        return

    target_name = rooms[room_id][target_sid]['name']

    # Tell the target their mic is being muted
    emit('you_were_muted', {
        'by':      _session['sub'],
        'message': f"You were muted by {_session['sub']}",
    }, to=target_sid)

    # Tell everyone else to show a mute indicator on that tile
    emit('participant_muted', {
        'peerId':   target_sid,
        'name':     target_name,
        'isMuted':  True,
        'by':       _session['sub'],
    }, to=room_id, skip_sid=target_sid)

    # Persist mute state to DB
    db.set_participant_muted(cfg, room_id, target_name, is_muted=True)

    logger.info(f"[HOST] '{_session['sub']}' muted '{target_name}' in '{room_id}'")


@socketio.on('unmute_participant')
@require_auth
def handle_unmute_participant(data, _session):
    """
    Host removes the force-mute from a participant.
    Note: this sends a REQUEST to unmute — the participant's client
    re-enables the mic. We cannot force a browser to open a mic without
    user consent (browser security policy).
    """
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')

    if room_admins.get(room_id) != sid:
        emit('error', {'code': 'NOT_ADMIN', 'message': 'Only the host can unmute participants.'})
        return

    if target_sid not in rooms.get(room_id, {}):
        return

    target_name = rooms[room_id][target_sid]['name']

    emit('you_were_unmuted', {
        'by':      _session['sub'],
        'message': f"You were unmuted by {_session['sub']}",
    }, to=target_sid)

    emit('participant_muted', {
        'peerId':  target_sid,
        'name':    target_name,
        'isMuted': False,
        'by':      _session['sub'],
    }, to=room_id, skip_sid=target_sid)

    db.set_participant_muted(cfg, room_id, target_name, is_muted=False)

    logger.info(f"[HOST] '{_session['sub']}' unmuted '{target_name}' in '{room_id}'")


def _admit_to_room(sid: str, name: str, room_id: str):
    """
    Admit a user to the room.
    FEATURE 1: Sends chat history to the newly admitted user.
    FEATURE 3: Sends current mute states to the newly admitted user.
    """
    if sid in rooms.get(room_id, {}):
        logger.warning(f"[ADMIT] Duplicate call for sid={sid} — ignoring")
        return

    join_room(room_id)

    existing = [
        {'peerId': peer_sid, 'name': peer_info['name']}
        for peer_sid, peer_info in rooms.get(room_id, {}).items()
    ]

    rooms[room_id][sid] = {
        'name':      name,
        'joined_at': time.time(),
        'is_admin':  room_admins.get(room_id) == sid,
    }

    emit('room_peers', {
        'peers':   existing,
        'isAdmin': room_admins.get(room_id) == sid,
    }, to=sid)

    emit('peer_joined', {'peerId': sid, 'name': name}, to=room_id, skip_sid=sid)

    # FEATURE 1: Send chat history to the newly admitted user
    history = db.get_room_history(cfg, room_id, limit=cfg.CHAT_HISTORY_LIMIT)
    if history:
        emit('chat_history', {'messages': history}, to=sid)

    # FEATURE 3: Send current host-mute states so the new user sees
    # who is already muted
    muted = db.get_muted_participants(cfg, room_id)
    if muted:
        emit('mute_state_snapshot', {'mutedNames': muted}, to=sid)

    logger.info(f"[JOIN] '{name}' admitted to '{room_id}' | {len(rooms[room_id])} in room")


# ── Chat ──────────────────────────────────────────────────────────────────────

@socketio.on('chat_message')
@require_auth
def handle_chat(data, _session):
    """
    FEATURE 1: Save message to MySQL before broadcasting.
    """
    sid     = request.sid
    room_id = _session['room']
    name    = _session['sub']
    message = str(data.get('message', '')).strip()[:500]
    if not message:
        return

    # FEATURE 1: Persist to MySQL
    db.save_message(cfg, room_id, sender_name=name, message=message)

    emit('chat_message', {
        'message':  message,
        'sender':   name,
        'senderId': sid,
    }, to=room_id, skip_sid=sid)


# ── Existing signaling events ─────────────────────────────────────────────────

@socketio.on('offer')
@require_auth
def handle_offer(data, _session):
    sid    = request.sid
    target = data.get('target')
    if not target or target not in rooms.get(_session['room'], {}):
        return
    emit('offer', {'sdp': data['sdp'], 'caller': sid}, to=target)


@socketio.on('answer')
@require_auth
def handle_answer(data, _session):
    sid    = request.sid
    target = data.get('target')
    if not target or target not in rooms.get(_session['room'], {}):
        return
    emit('answer', {'sdp': data['sdp'], 'answerer': sid}, to=target)


@socketio.on('ice_candidate')
@require_auth
def handle_ice_candidate(data, _session):
    target = data.get('target')
    if not target or target not in rooms.get(_session['room'], {}):
        return
    emit('ice_candidate', {'candidate': data.get('candidate'), 'sender': request.sid}, to=target)


@socketio.on('raise_hand')
@require_auth
def handle_raise_hand(data, _session):
    sid     = request.sid
    room_id = _session['room']
    emit('hand_raised', {
        'peerId': sid,
        'name':   _session['sub'],
        'raised': bool(data.get('raised', True)),
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logger.info(f"Starting MeetFree (booking-feature) on port {cfg.PORT}")
    logger.info(f"MySQL:  {'enabled' if cfg.MYSQL_ENABLED else 'disabled'}")
    logger.info(f"Redis:  {'enabled' if cfg.REDIS_URL else 'disabled'}")
    logger.info(f"Stripe: {'configured' if cfg.STRIPE_SECRET_KEY else 'disabled'}")
    socketio.run(app, host='0.0.0.0', port=cfg.PORT, debug=cfg.DEBUG)