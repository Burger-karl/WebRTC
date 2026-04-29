"""
server.py — MeetFree Production Signaling Server (hotfix branch)
Registers bookings_bp and admin_bp blueprints.
"""

import logging
import os
import time

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, join_room, emit, disconnect

from config import cfg
from auth import (
    create_token, validate_token_request,
    authenticate_socket, get_session, clear_session, require_auth,
)
from payments import payments_bp, is_subscribed
from bookings import bookings_bp
from admin import admin_bp
import database as db

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
app.register_blueprint(payments_bp)
app.register_blueprint(bookings_bp)
app.register_blueprint(admin_bp)

socketio = SocketIO(
    app,
    cors_allowed_origins=cfg.cors_origins,
    async_mode='eventlet',
    message_queue=cfg.socketio_message_queue,
    logger=cfg.DEBUG,
    engineio_logger=cfg.DEBUG,
)

rooms:       dict = {}
lobby:       dict = {}
room_admins: dict = {}
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


with app.app_context():
    db.init_db(cfg)


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
    ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    if not _check_rate_limit(ip):
        return jsonify({"error": "Too many requests."}), 429

    data     = request.get_json(silent=True) or {}
    name     = str(data.get("name",     "")).strip()
    room_id  = str(data.get("room",     "")).strip()
    email    = str(data.get("email",    "")).strip().lower()
    password = str(data.get("password", "")).strip()

    valid, error = validate_token_request(name, room_id)
    if not valid:
        return jsonify({"error": error}), 400

    if len(rooms.get(room_id, {})) >= cfg.MAX_USERS_PER_ROOM:
        return jsonify({"error": f"Room is full."}), 403

    if rooms.get(room_id):
        if not db.verify_room_password(cfg, room_id, password):
            return jsonify({"error": "Incorrect room password."}), 403

    if cfg.STRIPE_SECRET_KEY and email:
        allowed, reason = is_subscribed(email)
        if not allowed:
            return jsonify({"error": "Subscription expired.", "redirectTo": "/pricing"}), 402

    # Track user in DB
    if email:
        db.upsert_user(cfg, name, email)

    token = create_token(name, room_id)
    logger.info(f"[TOKEN] Issued: name='{name}' room='{room_id}' ip={ip}")
    return jsonify({"token": token, "iceServers": cfg.ice_servers(), "expiresIn": cfg.JWT_EXPIRY_SECONDS})


# ── Socket.IO ─────────────────────────────────────────────────────────────────

@socketio.on('connect')
def handle_connect(auth):
    if not authenticate_socket(request.sid, request.environ):
        return False


@socketio.on('disconnect')
def handle_disconnect():
    sid     = request.sid
    session_data = get_session(sid)
    name    = session_data.get('sub', 'unknown') if session_data else 'unknown'

    for room_id, peers in list(rooms.items()):
        if sid in peers:
            del peers[sid]
            emit('peer_left', {'peerId': sid}, to=room_id)
            if room_admins.get(room_id) == sid:
                del room_admins[room_id]
                if peers:
                    new_admin = next(iter(peers))
                    room_admins[room_id] = new_admin
                    emit('you_are_admin', {}, to=new_admin)
                    emit('admin_changed', {'newAdminId': new_admin}, to=room_id)
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


@socketio.on('request_join')
@require_auth
def handle_request_join(data, _session):
    sid     = request.sid
    name    = _session['sub']
    room_id = _session['room']
    password = str(data.get("password", "")).strip()

    if sid in rooms.get(room_id, {}):
        return

    room_is_empty = not rooms.get(room_id)

    if room_is_empty:
        rooms[room_id]       = {}
        room_admins[room_id] = sid
        db.create_room(cfg, room_id, created_by=name, password=password or None)
        _admit_to_room(sid, name, room_id)
        emit('you_are_admin', {})
    else:
        if room_id not in lobby:
            lobby[room_id] = {}
        lobby[room_id][sid] = {'name': name, 'requested_at': time.time()}
        join_room(f"lobby_{room_id}")
        emit('waiting_for_approval', {'message': 'Please wait — the host will let you in.'})
        admin_sid = room_admins.get(room_id)
        if admin_sid:
            emit('lobby_request', {'peerId': sid, 'name': name}, to=admin_sid)


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
        return
    room_lobby = lobby.get(room_id, {})
    if target_sid in room_lobby:
        del room_lobby[target_sid]
        if not room_lobby:
            lobby.pop(room_id, None)
        emit('admission_result', {'admitted': False, 'message': 'Entry denied.'}, to=target_sid)


@socketio.on('remove_participant')
@require_auth
def handle_remove_participant(data, _session):
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')
    if room_admins.get(room_id) != sid:
        return
    if target_sid in rooms.get(room_id, {}):
        emit('you_were_removed', {'message': 'You were removed by the host.'}, to=target_sid)


@socketio.on('mute_participant')
@require_auth
def handle_mute_participant(data, _session):
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')
    if room_admins.get(room_id) != sid:
        return
    if target_sid not in rooms.get(room_id, {}):
        return
    target_name = rooms[room_id][target_sid]['name']
    emit('you_were_muted', {'by': _session['sub'], 'message': f"You were muted by {_session['sub']}"}, to=target_sid)
    emit('participant_muted', {'peerId': target_sid, 'name': target_name, 'isMuted': True, 'by': _session['sub']}, to=room_id, skip_sid=target_sid)
    db.set_participant_muted(cfg, room_id, target_name, is_muted=True)


@socketio.on('unmute_participant')
@require_auth
def handle_unmute_participant(data, _session):
    sid        = request.sid
    room_id    = _session['room']
    target_sid = data.get('peerId')
    if room_admins.get(room_id) != sid:
        return
    if target_sid not in rooms.get(room_id, {}):
        return
    target_name = rooms[room_id][target_sid]['name']
    emit('you_were_unmuted', {'by': _session['sub'], 'message': f"You were unmuted by {_session['sub']}"}, to=target_sid)
    emit('participant_muted', {'peerId': target_sid, 'name': target_name, 'isMuted': False, 'by': _session['sub']}, to=room_id, skip_sid=target_sid)
    db.set_participant_muted(cfg, room_id, target_name, is_muted=False)


def _admit_to_room(sid: str, name: str, room_id: str):
    if sid in rooms.get(room_id, {}):
        return
    join_room(room_id)
    existing = [{'peerId': k, 'name': v['name']} for k, v in rooms.get(room_id, {}).items()]
    rooms[room_id][sid] = {'name': name, 'joined_at': time.time(), 'is_admin': room_admins.get(room_id) == sid}
    emit('room_peers', {'peers': existing, 'isAdmin': room_admins.get(room_id) == sid}, to=sid)
    emit('peer_joined', {'peerId': sid, 'name': name}, to=room_id, skip_sid=sid)
    history = db.get_room_history(cfg, room_id, limit=cfg.CHAT_HISTORY_LIMIT)
    if history:
        emit('chat_history', {'messages': history}, to=sid)
    muted = db.get_muted_participants(cfg, room_id)
    if muted:
        emit('mute_state_snapshot', {'mutedNames': muted}, to=sid)


@socketio.on('chat_message')
@require_auth
def handle_chat(data, _session):
    sid     = request.sid
    room_id = _session['room']
    message = str(data.get('message', '')).strip()[:500]
    if not message:
        return
    db.save_message(cfg, room_id, sender_name=_session['sub'], message=message)
    emit('chat_message', {'message': message, 'sender': _session['sub'], 'senderId': sid}, to=room_id, skip_sid=sid)


@socketio.on('offer')
@require_auth
def handle_offer(data, _session):
    target = data.get('target')
    if not target or target not in rooms.get(_session['room'], {}):
        return
    emit('offer', {'sdp': data['sdp'], 'caller': request.sid}, to=target)


@socketio.on('answer')
@require_auth
def handle_answer(data, _session):
    target = data.get('target')
    if not target or target not in rooms.get(_session['room'], {}):
        return
    emit('answer', {'sdp': data['sdp'], 'answerer': request.sid}, to=target)


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
    room_id = _session['room']
    emit('hand_raised', {'peerId': request.sid, 'name': _session['sub'], 'raised': bool(data.get('raised', True))}, to=room_id, skip_sid=request.sid)


@socketio.on('media_state')
@require_auth
def handle_media_state(data, _session):
    room_id = _session['room']
    emit('peer_media_state', {'peerId': request.sid, 'audioOn': bool(data.get('audioOn', True)), 'videoOn': bool(data.get('videoOn', True))}, to=room_id, skip_sid=request.sid)


if __name__ == '__main__':
    logger.info(f"Starting MeetFree on port {cfg.PORT}")
    logger.info(f"MySQL:  {cfg.MYSQL_HOST}:{cfg.MYSQL_PORT} | enabled={cfg.MYSQL_ENABLED}")
    logger.info(f"Redis:  {'enabled' if cfg.REDIS_URL else 'disabled'}")
    logger.info(f"Stripe: {'configured' if cfg.STRIPE_SECRET_KEY else 'disabled'}")
    logger.info(f"Admin:  /admin/dashboard (user: {cfg.ADMIN_USERNAME})")
    socketio.run(app, host='0.0.0.0', port=cfg.PORT, debug=cfg.DEBUG)