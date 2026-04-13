
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

# ── Socket.IO ─────────────────────────────────────────────────────────────────
socketio = SocketIO(
    app,
    cors_allowed_origins=cfg.cors_origins,
    async_mode='eventlet',
    message_queue=cfg.socketio_message_queue,
    logger=cfg.DEBUG,
    engineio_logger=cfg.DEBUG,
)

# ── State ─────────────────────────────────────────────────────────────────────
# BUG 1 FIX: Use plain dicts instead of defaultdict.
# defaultdict(dict) auto-creates a key on any read access, including the
# "if not rooms[room_id]" empty-room check. That silently created a new
# empty entry for every user who arrived at an empty room, causing every
# one of them to pass the "empty room" check and become admin.
# With a plain dict, we use .get() for reads and only write explicitly.

# rooms[room_id] = { sid: { name, joined_at, is_admin } }
rooms: dict = {}

# lobby[room_id] = { sid: { name, requested_at } }
lobby: dict = {}

# room_admins[room_id] = sid
room_admins: dict = {}

# Rate limiter: { ip: [timestamps] }
_rate_buckets: dict = {}


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    bucket = _rate_buckets.get(ip, [])
    bucket = [t for t in bucket if now - t < 60]
    if len(bucket) >= cfg.RATE_LIMIT_PER_MINUTE:
        _rate_buckets[ip] = bucket
        return False
    bucket.append(now)
    _rate_buckets[ip] = bucket
    return True


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
    })


@app.route('/api/token', methods=['POST'])
def issue_token():
    """Issue a signed JWT. Checks Stripe subscription if configured."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ip = ip.split(',')[0].strip()

    if not _check_rate_limit(ip):
        return jsonify({"error": "Too many requests. Please try again."}), 429

    data    = request.get_json(silent=True) or {}
    name    = str(data.get("name", "")).strip()
    room_id = str(data.get("room", "")).strip()
    email   = str(data.get("email", "")).strip().lower()

    valid, error = validate_token_request(name, room_id)
    if not valid:
        return jsonify({"error": error}), 400

    # BUG 1 FIX: use .get() not direct key access
    if len(rooms.get(room_id, {})) >= cfg.MAX_USERS_PER_ROOM:
        return jsonify({"error": f"Room is full (max {cfg.MAX_USERS_PER_ROOM})."}), 403

    if cfg.STRIPE_SECRET_KEY and email:
        allowed, reason = is_subscribed(email)
        if not allowed:
            return jsonify({
                "error":      "Your subscription has expired.",
                "reason":     reason,
                "redirectTo": "/pricing",
            }), 402
        logger.info(f"[TOKEN] Subscription OK for {email}: {reason}")

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

    # BUG 1 FIX: iterate over a snapshot with list(); use plain dict so no
    # phantom keys are created. Only rooms that actually contain this sid
    # will match — no ghost empty rooms to iterate over.
    for room_id, peers in list(rooms.items()):
        if sid in peers:
            del peers[sid]
            emit('peer_left', {'peerId': sid}, to=room_id)
            logger.info(f"[LEAVE] '{name}' left '{room_id}' | {len(peers)} remaining")

            # If the admin left, promote the next peer
            if room_admins.get(room_id) == sid:
                del room_admins[room_id]
                if peers:
                    new_admin_sid = next(iter(peers))
                    room_admins[room_id] = new_admin_sid
                    emit('you_are_admin', {}, to=new_admin_sid)
                    emit('admin_changed', {'newAdminId': new_admin_sid}, to=room_id)
                    logger.info(f"[LOBBY] New admin: {peers[new_admin_sid]['name']} in '{room_id}'")

            # Clean up empty rooms
            if not peers:
                del rooms[room_id]
                lobby.pop(room_id, None)
                room_admins.pop(room_id, None)
            break

    # Remove from lobby if they disconnected while waiting
    for room_id, waiters in list(lobby.items()):
        if sid in waiters:
            del waiters[sid]
            logger.info(f"[LOBBY] '{name}' left lobby for '{room_id}'")
            # Clean up empty lobby entries
            if not waiters:
                lobby.pop(room_id, None)
            break

    clear_session(sid)


# ── Socket.IO: Lobby (Waiting Room) ──────────────────────────────────────────

@socketio.on('request_join')
@require_auth
def handle_request_join(data, _session):
    """
    User requests entry to a room.

    BUG 1 FIX: Use rooms.get(room_id) instead of rooms[room_id].
    The old code used rooms[room_id] on a defaultdict, which auto-created
    an empty dict for any new room_id. Every arriving user saw an empty
    dict and became admin. Now we read with .get() which never creates keys.

    BUG 2 FIX: Legacy 'join' handler removed. Only 'request_join' exists.
    This eliminates the double-admission path where a reconnecting client
    fired both events and got admitted twice.
    """
    sid     = request.sid
    name    = _session['sub']
    room_id = _session['room']

    # BUG 3 FIX: early exit if this sid is somehow already in the room
    # (defensive guard against any double-fire edge case)
    if sid in rooms.get(room_id, {}):
        logger.warning(f"[JOIN] '{name}' (sid={sid}) already in '{room_id}' — ignoring duplicate request_join")
        return

    # BUG 1 FIX: rooms.get() never creates a phantom empty entry
    room_is_empty = not rooms.get(room_id)

    if room_is_empty:
        # First joiner: create the room and become admin
        rooms[room_id] = {}
        room_admins[room_id] = sid
        _admit_to_room(sid, name, room_id)
        emit('you_are_admin', {})
        logger.info(f"[LOBBY] '{name}' created room '{room_id}' as admin")
    else:
        # Room exists: send to lobby
        if room_id not in lobby:
            lobby[room_id] = {}
        lobby[room_id][sid] = {
            'name':         name,
            'requested_at': time.time(),
        }
        join_room(f"lobby_{room_id}")
        emit('waiting_for_approval', {
            'message': 'Please wait — the host will let you in shortly.',
        })
        # Notify the admin
        admin_sid = room_admins.get(room_id)
        if admin_sid:
            emit('lobby_request', {'peerId': sid, 'name': name}, to=admin_sid)
        logger.info(f"[LOBBY] '{name}' is waiting to join '{room_id}'")


@socketio.on('admit_user')
@require_auth
def handle_admit_user(data, _session):
    """Admin admits a waiting user into the room."""
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

    waiter_name = waiter['name']

    emit('admission_result', {
        'admitted': True,
        'message':  'You have been admitted to the meeting.',
    }, to=target_sid)

    _admit_to_room(target_sid, waiter_name, room_id)

    logger.info(f"[LOBBY] '{_session['sub']}' admitted '{waiter_name}' to '{room_id}'")


@socketio.on('deny_user')
@require_auth
def handle_deny_user(data, _session):
    """Admin denies a waiting user."""
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')

    if room_admins.get(room_id) != sid:
        emit('error', {'code': 'NOT_ADMIN', 'message': 'Only the host can deny users.'})
        return

    room_lobby = lobby.get(room_id, {})
    if target_sid in room_lobby:
        waiter_name = room_lobby[target_sid]['name']
        del room_lobby[target_sid]
        if not room_lobby:
            lobby.pop(room_id, None)

        emit('admission_result', {
            'admitted': False,
            'message':  'The host has denied your request to join.',
        }, to=target_sid)
        logger.info(f"[LOBBY] '{_session['sub']}' denied '{waiter_name}' from '{room_id}'")


@socketio.on('remove_participant')
@require_auth
def handle_remove_participant(data, _session):
    """Admin removes (kicks) a live participant."""
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')

    if room_admins.get(room_id) != sid:
        emit('error', {'code': 'NOT_ADMIN', 'message': 'Only the host can remove participants.'})
        return

    if target_sid in rooms.get(room_id, {}):
        emit('you_were_removed', {
            'message': 'You have been removed from the meeting by the host.'
        }, to=target_sid)
        logger.info(f"[LOBBY] '{_session['sub']}' removed "
                    f"'{rooms[room_id][target_sid]['name']}' from '{room_id}'")


def _admit_to_room(sid: str, name: str, room_id: str):
    """
    Add a user to the live room and notify everyone.

    BUG 3 FIX: Guard at top — if this sid is already registered in the room
    dict, exit immediately. This makes admission idempotent so that any
    edge-case double-call (e.g. network retry) cannot create a duplicate
    peer entry, duplicate room_peers emission, or duplicate peer_joined
    broadcast to existing participants.
    """
    # Duplicate-admission guard (Bug 3 fix)
    if sid in rooms.get(room_id, {}):
        logger.warning(f"[ADMIT] Duplicate _admit_to_room call for sid={sid} in '{room_id}' — ignoring")
        return

    join_room(room_id)

    # Snapshot of existing peers BEFORE adding the new one
    existing = [
        {'peerId': peer_sid, 'name': peer_info['name']}
        for peer_sid, peer_info in rooms.get(room_id, {}).items()
    ]

    # Write to the rooms dict (only place we ever write to it)
    rooms[room_id][sid] = {
        'name':      name,
        'joined_at': time.time(),
        'is_admin':  room_admins.get(room_id) == sid,
    }

    # Tell the new user who is already here
    emit('room_peers', {
        'peers':   existing,
        'isAdmin': room_admins.get(room_id) == sid,
    }, to=sid)

    # Tell existing peers a new user joined
    emit('peer_joined', {
        'peerId': sid,
        'name':   name,
    }, to=room_id, skip_sid=sid)

    logger.info(f"[JOIN] '{name}' (sid={sid}) admitted to '{room_id}' | {len(rooms[room_id])} in room")


# ── Signaling events ──────────────────────────────────────────────────────────

# BUG 2 FIX: The legacy 'join' handler that called handle_request_join
# internally has been REMOVED. It created a double-admission path:
# if a client reconnected and fired both 'join' AND 'request_join',
# the same user would be admitted twice. All clients now use 'request_join'.

@socketio.on('offer')
@require_auth
def handle_offer(data, _session):
    sid     = request.sid
    room_id = _session['room']
    target  = data.get('target')
    if not target or target not in rooms.get(room_id, {}):
        return
    emit('offer', {'sdp': data['sdp'], 'caller': sid}, to=target)


@socketio.on('answer')
@require_auth
def handle_answer(data, _session):
    sid     = request.sid
    room_id = _session['room']
    target  = data.get('target')
    if not target or target not in rooms.get(room_id, {}):
        return
    emit('answer', {'sdp': data['sdp'], 'answerer': sid}, to=target)


@socketio.on('ice_candidate')
@require_auth
def handle_ice_candidate(data, _session):
    room_id = _session['room']
    target  = data.get('target')
    if not target or target not in rooms.get(room_id, {}):
        return
    emit('ice_candidate', {
        'candidate': data.get('candidate'),
        'sender':    request.sid,
    }, to=target)


@socketio.on('chat_message')
@require_auth
def handle_chat(data, _session):
    sid     = request.sid
    room_id = _session['room']
    message = str(data.get('message', '')).strip()[:500]
    if not message:
        return
    emit('chat_message', {
        'message':  message,
        'sender':   _session['sub'],
        'senderId': sid,
    }, to=room_id, skip_sid=sid)


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
    logger.info(f"Starting MeetFree on port {cfg.PORT}")
    logger.info(f"Redis:  {'enabled - ' + cfg.REDIS_URL if cfg.REDIS_URL else 'disabled (single-worker)'}")
    logger.info(f"TURN:   {'configured' if cfg.TURN_URL else 'STUN only'}")
    logger.info(f"Stripe: {'configured' if cfg.STRIPE_SECRET_KEY else 'NOT configured (payments disabled)'}")
    socketio.run(app, host='0.0.0.0', port=cfg.PORT, debug=cfg.DEBUG)