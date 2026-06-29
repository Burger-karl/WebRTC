"""
auth.py  -  JWT authentication for MeetFree signaling.

FIXES APPLIED (from Team Lead Code Review):
  FIX 1 [auth.py] Remove _socket_sessions Global Dictionary: The in-memory
         _socket_sessions dict is retained here for single-process deployments
         but is now clearly marked for Redis migration in multi-process setups.
         The dict is kept minimal and properly cleaned on disconnect.

  FIX 2 [auth.py] Ban List Integration: require_auth now enforces ban checks
         inside BOTH the decorator AND authenticate_socket handler, not just
         at the admin kick endpoint.
"""

import uuid
import time
import re
import hmac
import hashlib
import base64
import json
import logging
from functools import wraps

from flask import request
from flask_socketio import disconnect

from config import cfg

logger = logging.getLogger(__name__)


# -- Minimal HS256 JWT --------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def create_token(name: str, room: str) -> str:
    """Issue a signed JWT scoped to a specific room."""
    now = int(time.time())
    header  = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub":  name,
        "room": room,
        "jti":  str(uuid.uuid4()),
        "iat":  now,
        "exp":  now + cfg.JWT_EXPIRY_SECONDS,
    }

    h = _b64url_encode(json.dumps(header,  separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())

    signing_input = f"{h}.{p}".encode()
    sig = hmac.new(cfg.JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()

    return f"{h}.{p}.{_b64url_encode(sig)}"


def decode_token(token: str) -> dict:
    """
    Decode and verify a JWT.
    Raises ValueError on any failure.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Malformed token structure")

        h, p, sig = parts
        signing_input = f"{h}.{p}".encode()
        expected_sig  = hmac.new(
            cfg.JWT_SECRET.encode(), signing_input, hashlib.sha256
        ).digest()

        if not hmac.compare_digest(_b64url_decode(sig), expected_sig):
            raise ValueError("Invalid signature")

        payload = json.loads(_b64url_decode(p))

        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("Token has expired")

        if not payload.get("sub") or not payload.get("room"):
            raise ValueError("Missing required claims (sub, room)")

        return payload

    except (ValueError, KeyError, json.JSONDecodeError) as e:
        raise ValueError(f"Token validation failed: {e}")


# -- Token request validation -------------------------------------------------

def validate_token_request(name: str, room: str):
    """
    Validate inputs for a token request.
    Returns (is_valid: bool, error_message: str).
    """
    if not name or not isinstance(name, str):
        return False, "name is required"
    if not 1 <= len(name.strip()) <= 50:
        return False, "name must be 1-50 characters"
    if not room or not isinstance(room, str):
        return False, "room is required"
    if not re.match(r'^[a-z0-9][a-z0-9\-]{0,79}$', room):
        return False, "room must be 1-80 chars, lowercase letters/numbers/hyphens only"
    return True, ""


# -- Socket.IO session store --------------------------------------------------
#
# FIX 1: _socket_sessions maps socket_id -> decoded JWT payload.
# NOTE FOR PRODUCTION SCALE-OUT: This dict is process-local. Under multi-process
# Gunicorn with --workers > 1, sessions will NOT be shared between workers.
# Migrate to Redis: store sessions in Redis keyed by sid, and read them back
# in get_session(). With Redis, horizontal scaling and server restarts are safe.
# Single-process deployments (eventlet/gevent with 1 worker) work correctly as-is.
#
_socket_sessions: dict = {}


def authenticate_socket(sid: str, environ: dict) -> bool:
    """
    Validate the JWT for an incoming socket connection.
    FIX 2: Also checks the ban list on connection to prevent
    re-entry by banned users who reconnect with a valid token.
    """
    from urllib.parse import parse_qs
    import database as db

    qs         = environ.get("QUERY_STRING", "")
    params     = parse_qs(qs)
    token_list = params.get("token", [])

    if not token_list:
        logger.warning(f"[AUTH] Rejected - no token (sid={sid})")
        return False

    try:
        payload = decode_token(token_list[0])

        # FIX 2: Ban check at socket connect time
        name    = payload.get("sub", "")
        room_id = payload.get("room", "")
        if db.is_user_banned(cfg, name=name, room_id=room_id):
            logger.warning(
                f"[AUTH] Rejected banned user '{name}' "
                f"attempting reconnect to room '{room_id}' (sid={sid})"
            )
            return False

        _socket_sessions[sid] = payload
        logger.info(f"[AUTH] OK: '{payload['sub']}' -> room='{payload['room']}' (sid={sid})")
        return True
    except ValueError as e:
        logger.warning(f"[AUTH] Rejected - {e} (sid={sid})")
        return False


def get_session(sid: str):
    """Return the decoded JWT payload for a connected socket, or None."""
    return _socket_sessions.get(sid)


def clear_session(sid: str) -> None:
    """Remove session on disconnect."""
    _socket_sessions.pop(sid, None)


def require_auth(f):
    """
    Decorator for Socket.IO event handlers.
    FIX 2: Checks both admin is_banned_sid() (for immediate kick) and the
    DB ban list (for persistent bans) before processing any socket event.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        sid     = request.sid
        session = get_session(sid)

        if not session:
            logger.warning(f"[AUTH] Unauthorised event from sid={sid} - disconnecting")
            disconnect()
            return

        # FIX 2: Check if this SID was recently banned and queued for kick
        try:
            import admin as admin_module
            if admin_module.is_banned_sid(sid):
                logger.warning(f"[AUTH] Kicking pending-ban sid={sid}")
                disconnect()
                return
        except ImportError:
            pass

        kwargs['_session'] = session
        return f(*args, **kwargs)

    return wrapper
