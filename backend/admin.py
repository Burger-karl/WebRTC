"""
admin.py — Role-Based Access Control (RBAC) Admin Panel for MeetFree
═══════════════════════════════════════════════════════════════════════
Implements two distinct roles:

  Super Admin
    - Full CRUD: ban/unban users, delete Room Admins
    - Global stats: total registered users, paid subscribers
    - All active rooms + all users across every room
    - System metrics: Python process CPU/memory only (not the host laptop)

  Room Admin
    - Sees only their own assigned room
    - Room subscription expiry / status
    - List of users inside their room only
    - Cannot see other rooms or global stats

Authentication:
    Every admin API endpoint requires:
        Authorization: Bearer <ADMIN_JWT>

    The ADMIN_JWT is a standard JWT whose payload carries:
        { "sub": "admin@example.com", "role": "super_admin"|"room_admin",
          "room": "<room_id>|null", "jti": "...", "iat": ..., "exp": ... }

    Tokens are issued via POST /api/admin/login using credentials stored
    in environment variables (see config.py additions).

Environment variables (add to .env):
    ADMIN_SECRET_KEY          — long random string for signing admin JWTs
    SUPER_ADMIN_EMAIL         — super admin login email
    SUPER_ADMIN_PASSWORD_HASH — bcrypt hash of super admin password
    ADMIN_JWT_EXPIRY_SECONDS  — default 3600 (1 hour)

Routes:
    POST /api/admin/login              — issue admin JWT
    GET  /api/admin/me                 — return current admin info
    GET  /api/admin/stats              — Super Admin: global stats
    GET  /api/admin/rooms              — Super Admin: all active rooms
    GET  /api/admin/room/<room_id>     — Room Admin: their room only
    GET  /api/admin/system             — Super Admin: Python process metrics
    POST /api/admin/users/<sid>/ban    — Super Admin: ban (kick + block) a user
    DELETE /api/admin/users/<email>    — Super Admin: delete user subscription
    GET  /api/admin/room-admins        — Super Admin: list Room Admins in DB
    POST /api/admin/room-admins        — Super Admin: create Room Admin account
    DELETE /api/admin/room-admins/<email> — Super Admin: remove Room Admin
    GET  /admin                        — Super Admin dashboard UI
    GET  /room-admin                   — Room Admin dashboard UI
"""

import logging
import time
import os
import uuid
import hmac
import hashlib
import base64
import json
from functools import wraps

import psutil
from flask import Blueprint, request, jsonify, render_template, g

from config import cfg
import database as db

logger = logging.getLogger("meetfree.admin")

admin_bp = Blueprint("admin", __name__)

# ── Token helpers ─────────────────────────────────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def _create_admin_token(email: str, role: str, room_id: str = None) -> str:
    """Issue a signed admin JWT."""
    now = int(time.time())
    expiry = int(os.getenv("ADMIN_JWT_EXPIRY_SECONDS", 3600))
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub":  email,
        "role": role,           # "super_admin" | "room_admin"
        "room": room_id,        # None for super_admin
        "jti":  str(uuid.uuid4()),
        "iat":  now,
        "exp":  now + expiry,
    }
    h = _b64url_encode(json.dumps(header,  separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()
    secret = os.getenv("ADMIN_SECRET_KEY", cfg.SECRET_KEY + "-admin")
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"


def _decode_admin_token(token: str) -> dict:
    """Decode and verify an admin JWT. Raises ValueError on failure."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Malformed token")
        h, p, sig = parts
        signing_input = f"{h}.{p}".encode()
        secret = os.getenv("ADMIN_SECRET_KEY", cfg.SECRET_KEY + "-admin")
        expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url_decode(sig), expected):
            raise ValueError("Invalid signature")
        payload = json.loads(_b64url_decode(p))
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("Token expired")
        if not payload.get("sub") or not payload.get("role"):
            raise ValueError("Missing claims")
        return payload
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        raise ValueError(f"Admin token invalid: {e}")


# ── Auth decorators ───────────────────────────────────────────────────────────

def _get_admin_session() -> dict | None:
    """Extract and validate the Bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        return _decode_admin_token(auth[7:])
    except ValueError:
        return None


def require_admin(f):
    """Require any valid admin token (Super Admin OR Room Admin)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        session = _get_admin_session()
        if not session:
            return jsonify({"error": "Unauthorized — valid admin token required"}), 401
        g.admin = session
        return f(*args, **kwargs)
    return wrapper


def require_super_admin(f):
    """Require Super Admin role specifically."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        session = _get_admin_session()
        if not session:
            return jsonify({"error": "Unauthorized"}), 401
        if session.get("role") != "super_admin":
            return jsonify({"error": "Forbidden — Super Admin access required"}), 403
        g.admin = session
        return f(*args, **kwargs)
    return wrapper


# ── Process-level metrics (Python server only, NOT the laptop) ────────────────

_server_start_time = time.time()
_own_process = psutil.Process(os.getpid())


def _get_process_metrics() -> dict:
    """
    Return resource usage for THIS Python process only.
    Compatible with psutil 5.x and 6.x on Windows, Linux, and Mac.
    """
    try:
        with _own_process.oneshot():
            cpu_pct    = _own_process.cpu_percent(interval=0.1)
            mem_info   = _own_process.memory_info()
            mem_mb     = round(mem_info.rss / 1024 / 1024, 2)
            mem_vms_mb = round(mem_info.vms / 1024 / 1024, 2)
            threads    = _own_process.num_threads()

            # num_fds is Unix-only; Windows uses num_handles
            if hasattr(_own_process, "num_fds"):
                fds = _own_process.num_fds()
            elif hasattr(_own_process, "num_handles"):
                fds = _own_process.num_handles()
            else:
                fds = 0

            # open_files can raise AccessDenied on Windows
            try:
                open_files = len(_own_process.open_files())
            except (psutil.AccessDenied, OSError):
                open_files = 0

            # net_connections() was renamed to connections() in psutil 6.0
            try:
                if hasattr(_own_process, "connections"):
                    conns = len(_own_process.connections())
                else:
                    conns = len(_own_process.net_connections())
            except (psutil.AccessDenied, OSError):
                conns = 0

        uptime_secs = int(time.time() - _server_start_time)
        uptime_str  = _format_uptime(uptime_secs)

        return {
            "pid":                 _own_process.pid,
            "uptime_seconds":      uptime_secs,
            "uptime_human":        uptime_str,
            "cpu_percent":         cpu_pct,
            "memory_rss_mb":       mem_mb,
            "memory_vms_mb":       mem_vms_mb,
            "threads":             threads,
            "open_file_handles":   fds,
            "open_files":          open_files,
            "network_connections": conns,
            "note": "Metrics are for the Python server process only, not the host machine.",
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        return {"error": f"Process metrics unavailable: {e}"}


def _format_uptime(secs: int) -> str:
    days    = secs // 86400
    hours   = (secs % 86400) // 3600
    minutes = (secs % 3600) // 60
    seconds = secs % 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


# ── Shared rooms reference (injected at startup from server.py) ───────────────
# server.py calls:  admin.inject_rooms(rooms, room_admins)
_rooms_ref:       dict = {}
_room_admins_ref: dict = {}


def inject_rooms(rooms: dict, room_admins: dict):
    """Called once from server.py so admin module has live access to room state."""
    global _rooms_ref, _room_admins_ref
    _rooms_ref       = rooms
    _room_admins_ref = room_admins


# ── Login ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/login", methods=["POST"])
def admin_login():
    """
    Issue an admin JWT.

    Request: { "email": "...", "password": "..." }
    Response: { "token": "...", "role": "...", "room": "..." }

    Credentials are checked against:
      1. Super Admin: SUPER_ADMIN_EMAIL + SUPER_ADMIN_PASSWORD_HASH env vars
      2. Room Admins: stored in MySQL via the room_admins table
    """
    data     = request.get_json(silent=True) or {}
    email    = str(data.get("email",    "")).strip().lower()
    password = str(data.get("password", "")).strip()

    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400

    # ── Check Super Admin credentials ─────────────────────────
    sa_email = os.getenv("SUPER_ADMIN_EMAIL", "").strip().lower()
    # Strip ALL whitespace including Windows \r\n line endings that corrupt the hash
    sa_hash  = os.getenv("SUPER_ADMIN_PASSWORD_HASH", "").strip()

    if email == sa_email and sa_hash:
        import bcrypt
        # Validate hash format before calling checkpw — prevents ValueError: Invalid salt
        if not sa_hash.startswith("$2b$") and not sa_hash.startswith("$2a$"):
            logger.error(
                f"[ADMIN] SUPER_ADMIN_PASSWORD_HASH is not a valid bcrypt hash. "
                f"First 10 chars: '{sa_hash[:10]}'. "
                f"Run: python create_superadmin.py to regenerate it."
            )
            return jsonify({"error": "Server misconfiguration — admin hash is invalid. Check server logs."}), 500
        try:
            if bcrypt.checkpw(password.encode(), sa_hash.encode()):
                token = _create_admin_token(email, "super_admin", room_id=None)
                logger.info(f"[ADMIN] Super Admin login: {email}")
                return jsonify({"token": token, "role": "super_admin", "room": None})
        except ValueError as e:
            logger.error(f"[ADMIN] bcrypt error for super admin: {e} | hash starts with: '{sa_hash[:15]}'")
            return jsonify({"error": "Server misconfiguration — admin hash is corrupted. Check server logs."}), 500

    # ── Check Room Admin credentials ──────────────────────────
    room_admin = db.get_room_admin_by_email(cfg, email)
    if room_admin:
        import bcrypt
        room_hash = (room_admin["password_hash"] or "").strip()
        if not room_hash.startswith("$2b$") and not room_hash.startswith("$2a$"):
            logger.error(f"[ADMIN] Room Admin hash invalid for {email}: '{room_hash[:10]}'")
        else:
            try:
                if bcrypt.checkpw(password.encode(), room_hash.encode()):
                    token = _create_admin_token(email, "room_admin", room_id=room_admin["room_id"])
                    logger.info(f"[ADMIN] Room Admin login: {email} → room={room_admin['room_id']}")
                    return jsonify({
                        "token": token,
                        "role":  "room_admin",
                        "room":  room_admin["room_id"],
                    })
            except ValueError as e:
                logger.error(f"[ADMIN] bcrypt error for room admin {email}: {e}")

    logger.warning(f"[ADMIN] Failed login attempt: {email}")
    return jsonify({"error": "Invalid credentials"}), 401


# ── Whoami ────────────────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/me", methods=["GET"])
@require_admin
def admin_me():
    """Return the current admin's info."""
    return jsonify({
        "email": g.admin["sub"],
        "role":  g.admin["role"],
        "room":  g.admin.get("room"),
    })


# ── Super Admin: Global stats ─────────────────────────────────────────────────

@admin_bp.route("/api/admin/stats", methods=["GET"])
@require_super_admin
def admin_global_stats():
    """
    Global platform statistics — Super Admin only.

    Returns:
      - active_rooms:       currently live rooms
      - active_users:       sum of all peers across all live rooms
      - lobby_waiting:      users waiting in lobby across all rooms
      - total_registered:   total user records ever created (from DB)
      - total_paid_subs:    total users with active/paid subscriptions (from DB)
      - rooms_detail:       list of room summaries with user counts
    """
    # Live counts come from the in-memory state — always accurate
    active_rooms = len(_rooms_ref)
    active_users = sum(len(peers) for peers in _rooms_ref.values())

    rooms_detail = []
    for room_id, peers in _rooms_ref.items():
        admin_sid  = _room_admins_ref.get(room_id)
        admin_name = peers.get(admin_sid, {}).get("name", "unknown") if admin_sid else "unknown"
        rooms_detail.append({
            "room_id":    room_id,
            "user_count": len(peers),
            "host":       admin_name,
            "users":      [
                {"sid": sid, "name": info["name"], "joined_at": info.get("joined_at")}
                for sid, info in peers.items()
            ],
        })

    # DB-level stats
    total_registered = db.count_registered_users(cfg)
    total_paid       = db.count_paid_subscribers(cfg)

    return jsonify({
        "active_rooms":    active_rooms,
        "active_users":    active_users,
        "total_registered": total_registered,
        "total_paid_subs":  total_paid,
        "rooms_detail":    rooms_detail,
    })


# ── Super Admin: All rooms ────────────────────────────────────────────────────

@admin_bp.route("/api/admin/rooms", methods=["GET"])
@require_super_admin
def admin_all_rooms():
    """Super Admin: list every active room with all users."""
    result = []
    for room_id, peers in _rooms_ref.items():
        admin_sid = _room_admins_ref.get(room_id)
        result.append({
            "room_id":    room_id,
            "user_count": len(peers),
            "host_sid":   admin_sid,
            "users": [
                {
                    "sid":       sid,
                    "name":      info["name"],
                    "joined_at": info.get("joined_at"),
                    "is_host":   sid == admin_sid,
                }
                for sid, info in peers.items()
            ],
        })
    return jsonify({"rooms": result, "total": len(result)})


# ── Room Admin: their room only ───────────────────────────────────────────────

@admin_bp.route("/api/admin/room/<room_id>", methods=["GET"])
@require_admin
def admin_room_detail(room_id):
    """
    Room Admin: fetch data for their assigned room only.
    Super Admin: can fetch any room.
    """
    session = g.admin

    # Room Admins can only see their own room
    if session["role"] == "room_admin" and session.get("room") != room_id:
        return jsonify({"error": "Forbidden — you can only view your assigned room"}), 403

    peers = _rooms_ref.get(room_id, {})
    admin_sid = _room_admins_ref.get(room_id)

    # Subscription / expiry info from DB
    room_record = db.get_room(cfg, room_id)
    sub_expiry  = None
    if room_record:
        sub_expiry = room_record.get("subscription_expires_at")

    return jsonify({
        "room_id":          room_id,
        "user_count":       len(peers),
        "subscription_expires_at": str(sub_expiry) if sub_expiry else None,
        "users": [
            {
                "sid":     sid,
                "name":    info["name"],
                "is_host": sid == admin_sid,
            }
            for sid, info in peers.items()
        ],
    })


# ── Super Admin: System metrics (Python process ONLY) ────────────────────────

@admin_bp.route("/api/admin/system", methods=["GET"])
@require_super_admin
def admin_system_metrics():
    """
    Return resource usage for THIS Python server process only.
    Does NOT include host machine totals — no disk, no whole-machine RAM.
    """
    return jsonify(_get_process_metrics())


# ── Super Admin: Ban a connected user ─────────────────────────────────────────

@admin_bp.route("/api/admin/users/<sid>/ban", methods=["POST"])
@require_super_admin
def admin_ban_user(sid):
    """
    Kick a connected socket by SID and add their name to the ban list.
    The actual socket disconnect is signalled via the socketio reference
    injected at startup.
    """
    # Find the user across all rooms
    found_room = None
    found_name = None
    for room_id, peers in _rooms_ref.items():
        if sid in peers:
            found_room = room_id
            found_name = peers[sid]["name"]
            break

    if not found_room:
        return jsonify({"error": "User not found in any active room"}), 404

    # Persist ban in DB (email unknown at socket level — ban by name+room for now)
    db.ban_user(cfg, name=found_name, room_id=found_room, banned_by=g.admin["sub"])

    # Signal the socket layer to kick this SID (server.py handles emission)
    # We store it so server.py can pick it up next time it processes that SID
    _pending_kicks.add(sid)

    logger.warning(f"[ADMIN] Super Admin '{g.admin['sub']}' banned '{found_name}' (sid={sid}) from '{found_room}'")
    return jsonify({"status": "banned", "name": found_name, "room": found_room})


# Pending SIDs to kick — server.py checks this set before processing events
_pending_kicks: set = set()


def is_banned_sid(sid: str) -> bool:
    """Called by server.py's require_auth to block banned users instantly."""
    if sid in _pending_kicks:
        _pending_kicks.discard(sid)
        return True
    return False


# ── Super Admin: Delete user subscription ────────────────────────────────────

@admin_bp.route("/api/admin/users/<email>", methods=["DELETE"])
@require_super_admin
def admin_delete_user(email):
    """Super Admin: remove a user's subscription record."""
    email = email.strip().lower()
    deleted = db.delete_user_subscription(cfg, email)
    if not deleted:
        return jsonify({"error": "User not found"}), 404
    logger.warning(f"[ADMIN] Super Admin '{g.admin['sub']}' deleted user '{email}'")
    return jsonify({"status": "deleted", "email": email})


# ── Super Admin: Room Admin CRUD ──────────────────────────────────────────────

@admin_bp.route("/api/admin/room-admins", methods=["GET"])
@require_super_admin
def list_room_admins():
    """Super Admin: list all Room Admin accounts."""
    admins = db.get_all_room_admins(cfg)
    return jsonify({"room_admins": admins})


@admin_bp.route("/api/admin/room-admins", methods=["POST"])
@require_super_admin
def create_room_admin():
    """
    Super Admin: create a Room Admin account.
    Request: { "email": "...", "password": "...", "room_id": "..." }
    """
    import bcrypt
    data     = request.get_json(silent=True) or {}
    email    = str(data.get("email",    "")).strip().lower()
    password = str(data.get("password", "")).strip()
    room_id  = str(data.get("room_id",  "")).strip()

    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if not room_id:
        return jsonify({"error": "room_id is required"}), 400

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    ok = db.create_room_admin(cfg, email=email, password_hash=pw_hash, room_id=room_id,
                               created_by=g.admin["sub"])
    if not ok:
        return jsonify({"error": "Room Admin already exists or DB error"}), 409

    logger.info(f"[ADMIN] Super Admin '{g.admin['sub']}' created Room Admin '{email}' → room='{room_id}'")
    return jsonify({"status": "created", "email": email, "room_id": room_id}), 201


@admin_bp.route("/api/admin/room-admins/<email>", methods=["DELETE"])
@require_super_admin
def delete_room_admin(email):
    """Super Admin: remove a Room Admin account."""
    email = email.strip().lower()
    ok = db.delete_room_admin(cfg, email)
    if not ok:
        return jsonify({"error": "Room Admin not found"}), 404
    logger.warning(f"[ADMIN] Super Admin '{g.admin['sub']}' deleted Room Admin '{email}'")
    return jsonify({"status": "deleted", "email": email})


# ── Dashboard HTML pages ──────────────────────────────────────────────────────

@admin_bp.route("/admin")
def super_admin_ui():
    """Serve the Super Admin dashboard HTML."""
    return render_template("super_admin.html")


@admin_bp.route("/room-admin")
def room_admin_ui():
    """Serve the Room Admin dashboard HTML."""
    return render_template("room_admin.html")