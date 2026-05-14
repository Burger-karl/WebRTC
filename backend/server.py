"""
server.py — MeetFree Signaling Server (RBAC + All Fixes branch)

CHANGES vs feature-order-booking-automation branch:

  FIX 1 — Active Users / Active Rooms always showed zero in admin dashboard.
       Root cause: The /health endpoint was correct, but the admin dashboard
       was not calling it. Now admin.inject_rooms() keeps admin.py in sync
       with the live in-memory `rooms` dict. Because both share the same
       dict reference, any mutation in server.py is instantly visible in
       admin.py without any extra call.

  FIX 2 — System metrics now show Python process only (not the laptop).
       Delegated entirely to admin._get_process_metrics() which uses
       psutil.Process(os.getpid()) — current process, not psutil.cpu_percent()
       which measures the whole machine.

  FIX 3 — Stripe redirect.
       The /api/create-order and /api/create-checkout endpoints already return
       { "url": "..." }. The frontend must redirect to data.url.
       Added STRIPE_SERVICE_PRICE_ID guard with a clear 503 message.

  FIX 4 — RBAC (Super Admin + Room Admin) via admin.py blueprint.
       Super Admin: global stats, all rooms, ban/delete users, CRUD room admins.
       Room Admin: only their room, only their users, expiry date.

  FIX 5 — User registration tracking.
       Every successful /api/token call calls db.upsert_user() so that
       'total registered users' and 'total paid subscribers' are accurate
       in the Super Admin stats panel.

  FIX 6 — Banned user enforcement.
       require_auth now checks admin.is_banned_sid() and db.is_user_banned()
       before processing any socket event from a banned user.
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
import admin as admin_module
from admin import admin_bp

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
app.register_blueprint(admin_bp)

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


# ── Initialise MySQL and inject shared state into admin module ────────────────
with app.app_context():
    db.init_db(cfg)

# FIX 1: Give admin module a live reference to the same dicts.
# Because Python dicts are passed by reference, any mutation to `rooms` or
# `room_admins` in this file is automatically visible inside admin.py.
admin_module.inject_rooms(rooms, room_admins)


# ── HTTP Routes ───────────────────────────────────────────────────────────────

@app.route('/')
def homepage():
    """New homepage with pricing — served at /"""
    from config import cfg
    return render_template(
        'homepage.html',
        price_monthly = cfg.STRIPE_PRICE_MONTHLY,
        price_yearly  = cfg.STRIPE_PRICE_YEARLY,
    )

@app.route('/join')
def join_page():
    """The original join/create room page — now at /join"""
    return render_template('index.html')


@app.route('/room/<room_id>')
def room(room_id):
    return render_template('room.html', room_id=room_id)


@app.route('/health')
def health():
    return jsonify({
        "status":        "ok",
        "active_rooms":  len(rooms),      # FIX 1: correct live count
        "active_peers":  sum(len(v) for v in rooms.values()),
        "lobby_waiting": sum(len(v) for v in lobby.values()),
        "redis":         bool(cfg.REDIS_URL),
        "mysql":         db._db_available,
    })


@app.route('/api/check-room', methods=['POST'])
def check_room():
    data    = request.get_json(silent=True) or {}
    room_id = str(data.get("room", "")).strip()
    if not room_id:
        return jsonify({"error": "room is required"}), 400

    room_is_live = bool(rooms.get(room_id))
    has_password = False
    if room_is_live and db._db_available:
        room_record  = db.get_room(cfg, room_id)
        has_password = bool(room_record and room_record.get('password_hash'))

    return jsonify({"exists": room_is_live, "hasPassword": has_password})


@app.route('/api/token', methods=['POST'])
def issue_token():
    """
    Issue a signed JWT.
    FIX 5: Calls db.upsert_user() so registered user count is always accurate.
    """
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ip = ip.split(',')[0].strip()

    if not _check_rate_limit(ip):
        return jsonify({"error": "Too many requests. Please try again."}), 429

    data     = request.get_json(silent=True) or {}
    name     = str(data.get("name",     "")).strip()
    room_id  = str(data.get("room",     "")).strip()
    email    = str(data.get("email",    "")).strip().lower()
    password = str(data.get("password", "")).strip()

    valid, error = validate_token_request(name, room_id)
    if not valid:
        return jsonify({"error": error}), 400

    if len(rooms.get(room_id, {})) >= cfg.MAX_USERS_PER_ROOM:
        return jsonify({"error": f"Room is full (max {cfg.MAX_USERS_PER_ROOM})."}), 403

    # Password check
    if rooms.get(room_id):
        if not db.verify_room_password(cfg, room_id, password):
            return jsonify({"error": "Incorrect room password."}), 403

    # Stripe subscription check
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

    # FIX 5: Track registration so super-admin stats are accurate
    if email:
        sub_status = "active" if cfg.STRIPE_SECRET_KEY and is_subscribed(email)[0] else "trial"
        db.upsert_user(cfg, email=email, display_name=name, sub_status=sub_status)

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

            if not peers:
                del rooms[room_id]
                lobby.pop(room_id, None)
                room_admins.pop(room_id, None)
                db.delete_room_history(cfg, room_id)
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
    sid      = request.sid
    name     = _session['sub']
    room_id  = _session['room']
    password = str(data.get("password", "")).strip()

    # FIX 6: Block banned users
    if db.is_user_banned(cfg, name=name, room_id=room_id):
        emit('error', {'code': 'BANNED', 'message': 'You have been banned from this room.'})
        disconnect()
        return

    if sid in rooms.get(room_id, {}):
        logger.warning(f"[JOIN] Duplicate request_join from '{name}' — ignoring")
        return

    room_is_empty = not rooms.get(room_id)

    if room_is_empty:
        rooms[room_id]       = {}
        room_admins[room_id] = sid
        db.create_room(cfg, room_id, created_by=name, password=password or None)
        _admit_to_room(sid, name, room_id)
        emit('you_are_admin', {})
        logger.info(f"[HOST] '{name}' created room '{room_id}'")
    else:
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


@socketio.on('remove_participant')
@require_auth
def handle_remove_participant(data, _session):
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


@socketio.on('mute_participant')
@require_auth
def handle_mute_participant(data, _session):
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')

    if room_admins.get(room_id) != sid:
        emit('error', {'code': 'NOT_ADMIN', 'message': 'Only the host can mute participants.'})
        return

    if target_sid not in rooms.get(room_id, {}):
        return

    target_name = rooms[room_id][target_sid]['name']
    emit('you_were_muted', {'by': _session['sub'], 'message': f"You were muted by {_session['sub']}"}, to=target_sid)
    emit('participant_muted', {'peerId': target_sid, 'name': target_name, 'isMuted': True, 'by': _session['sub']},
         to=room_id, skip_sid=target_sid)
    db.set_participant_muted(cfg, room_id, target_name, is_muted=True)


@socketio.on('unmute_participant')
@require_auth
def handle_unmute_participant(data, _session):
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')

    if room_admins.get(room_id) != sid:
        emit('error', {'code': 'NOT_ADMIN', 'message': 'Only the host can unmute participants.'})
        return

    if target_sid not in rooms.get(room_id, {}):
        return

    target_name = rooms[room_id][target_sid]['name']
    emit('you_were_unmuted', {'by': _session['sub']}, to=target_sid)
    emit('participant_muted', {'peerId': target_sid, 'name': target_name, 'isMuted': False, 'by': _session['sub']},
         to=room_id, skip_sid=target_sid)
    db.set_participant_muted(cfg, room_id, target_name, is_muted=False)


def _admit_to_room(sid: str, name: str, room_id: str):
    if sid in rooms.get(room_id, {}):
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

    emit('room_peers', {'peers': existing, 'isAdmin': room_admins.get(room_id) == sid}, to=sid)
    emit('peer_joined', {'peerId': sid, 'name': name}, to=room_id, skip_sid=sid)

    history = db.get_room_history(cfg, room_id, limit=cfg.CHAT_HISTORY_LIMIT)
    if history:
        emit('chat_history', {'messages': history}, to=sid)

    muted = db.get_muted_participants(cfg, room_id)
    if muted:
        emit('mute_state_snapshot', {'mutedNames': muted}, to=sid)

    logger.info(f"[JOIN] '{name}' admitted to '{room_id}' | {len(rooms[room_id])} in room")


# ── Chat ──────────────────────────────────────────────────────────────────────

@socketio.on('chat_message')
@require_auth
def handle_chat(data, _session):
    sid     = request.sid
    name    = _session['sub']
    # Use the room from JWT — but verify the socket is actually IN that room.
    # If the socket isn't in the room (mismatch between URL room_id and JWT room),
    # fall back to whichever room this socket actually joined.
    jwt_room = _session['room']
    if sid in rooms.get(jwt_room, {}):
        room_id = jwt_room
    else:
        # Find whichever room this SID is actually in
        room_id = None
        for rid, peers in rooms.items():
            if sid in peers:
                room_id = rid
                break
        if not room_id:
            logger.warning(f"[CHAT] '{name}' (sid={sid}) not in any room — dropping message")
            return

    message = str(data.get('message', '')).strip()[:500]
    if not message:
        return

    db.save_message(cfg, room_id, sender_name=name, message=message)
    # Use the context-aware `emit` (imported from flask_socketio), NOT
    # `socketio.emit()`. Inside a socket event handler, only the
    # context-aware emit guarantees delivery to the correct namespace/room.
    emit('chat_message',
         {'message': message, 'sender': name, 'senderId': sid},
         to=room_id)
    logger.debug(f"[CHAT] '{name}' in '{room_id}': {message[:50]}")


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
    logger.info(f"Starting MeetFree on port {cfg.PORT}")
    logger.info(f"MySQL:  {'enabled' if cfg.MYSQL_ENABLED else 'disabled'}")
    logger.info(f"Redis:  {'enabled' if cfg.REDIS_URL else 'disabled'}")
    logger.info(f"Stripe: {'configured' if cfg.STRIPE_SECRET_KEY else 'disabled'}")
    socketio.run(app, host='0.0.0.0', port=cfg.PORT, debug=cfg.DEBUG)