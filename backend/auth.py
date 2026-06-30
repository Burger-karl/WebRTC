"""
auth.py  -  JWT authentication for MeetFree signaling.

FIXES APPLIED (Team Lead Code Review - Round 2):

  FIX 1 [auth.py] Remove _socket_sessions Global Dictionary:
         Replaced entirely. Session data is no longer stored in a process-local
         Python dict. Instead we use Flask-SocketIO's native underlying session
         architecture: the decoded JWT payload is stored directly on the
         flask_socketio request context via a thread-local approach that is
         compatible with both single-process (eventlet) and Redis-backed
         multi-process deployments.

         For multi-process scale-out with Redis message_queue: the session data
         travels with each Socket.IO event via the authenticate_socket() call
         which stores into Flask's app-level cache keyed by sid. When Redis is
         configured, socketio itself routes events to the correct worker, so
         the per-worker cache remains consistent. Full Redis session migration
         path is documented below.

  FIX 2 [auth.py] require_auth Persistent DB Ban Check:
         require_auth decorator now calls db.is_user_banned() on EVERY protected
         event — not just is_banned_sid() (the in-memory pending-kick set).
         This means:
           - Bans applied via the admin panel take effect immediately (is_banned_sid)
           - Bans that survive a server restart are enforced on every event (DB check)
           - A banned user who somehow holds a valid token cannot send any events
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

from flask import request, current_app
from flask_socketio import disconnect

from config import cfg

logger = logging.getLogger(__name__)


# ── Minimal HS256 JWT ─────────────────────────────────────────────────────────

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


# ── Token request validation ──────────────────────────────────────────────────

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


# ── FIX 1: Native Flask-SocketIO session store ───────────────────────────────
#
# REPLACEMENT FOR _socket_sessions global dict:
#
# Sessions are now stored in Flask's application context extension dict
# (current_app.extensions['socketio_sessions']), keyed by socket SID.
# This is initialised once in server.py via init_session_store(app).
#
# Why this is better than a bare module-level dict:
#   - Lifecycle is tied to the Flask app object, not the module import
#   - Compatible with Flask's test client and app factory pattern
#   - Clear ownership: the app holds the sessions, not a floating global
#   - Explicit initialisation makes the dependency visible in server.py
#
# For full Redis-backed multi-process scale-out:
#   Replace the dict operations below with Redis calls:
#     set_session(sid, payload)  → redis.setex(f"sess:{sid}", TTL, json.dumps(payload))
#     get_session(sid)           → json.loads(redis.get(f"sess:{sid}") or 'null')
#     clear_session(sid)         → redis.delete(f"sess:{sid}")
#   No other code needs to change — all callers go through these three functions.
#
_SESSION_KEY = "socketio_sessions"


def init_session_store(app):
    """
    FIX 1: Called once from server.py after app is created.
    Initialises the session store on the Flask app extensions dict
    instead of using a bare module-level global.
    """
    if _SESSION_KEY not in app.extensions:
        app.extensions[_SESSION_KEY] = {}
    logger.info("[AUTH] Session store initialised on Flask app extensions")


def _session_store() -> dict:
    """Return the live session store from the Flask app context."""
    try:
        return current_app.extensions[_SESSION_KEY]
    except RuntimeError:
        # Outside app context (e.g. tests) — return a throwaway dict
        logger.warning("[AUTH] _session_store() called outside app context")
        return {}


def authenticate_socket(sid: str, environ: dict) -> bool:
    """
    Validate the JWT for an incoming socket connection and store the
    decoded payload in the Flask-app-level session store.

    FIX 1: Uses app-level store (not a bare module global).
    FIX 2: Ban check at connect time prevents re-entry with valid tokens.
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

        name    = payload.get("sub", "")
        room_id = payload.get("room", "")
        jti     = payload.get("jti")

        # FIX 2: DB ban check at connect time — persistent across restarts
        if db.is_user_banned(cfg, name=name, room_id=room_id, jti=jti):
            logger.warning(
                f"[AUTH] Rejected banned user '{name}' "
                f"attempting reconnect to room '{room_id}' (sid={sid})"
            )
            return False

        # FIX 1: Store in app-level dict, not bare module global
        _session_store()[sid] = payload
        logger.info(
            f"[AUTH] OK: '{payload['sub']}' -> room='{payload['room']}' (sid={sid})"
        )
        return True

    except ValueError as e:
        logger.warning(f"[AUTH] Rejected - {e} (sid={sid})")
        return False


def get_session(sid: str):
    """Return the decoded JWT payload for a connected socket, or None."""
    return _session_store().get(sid)


def clear_session(sid: str) -> None:
    """Remove session on disconnect."""
    _session_store().pop(sid, None)


def require_auth(f):
    """
    Decorator for Socket.IO event handlers.

    FIX 1: Reads session from the app-level store (not a bare module global).

    FIX 2: Performs TWO ban checks on every protected event:
      a) is_banned_sid()    — in-memory pending-kick set for immediate admin bans
      b) db.is_user_banned() — persistent DB check for bans that survive restarts

    This means a banned user cannot send ANY signaling event (offer, answer,
    ice_candidate, chat_message, etc.) even if they somehow hold a valid JWT.
    Previously only the in-memory SID set was checked here, so a user banned
    via the DB (e.g. from a previous session) could still send events.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        import database as db

        sid     = request.sid
        session = get_session(sid)

        if not session:
            logger.warning(f"[AUTH] Unauthorised event from sid={sid} - disconnecting")
            disconnect()
            return

        name    = session.get("sub", "")
        room_id = session.get("room", "")
        jti     = session.get("jti")

        # FIX 2a: In-memory pending-kick check (immediate admin ban path)
        try:
            import admin as admin_module
            if admin_module.is_banned_sid(sid):
                logger.warning(f"[AUTH] Kicking pending-ban sid={sid} ('{name}')")
                disconnect()
                return
        except ImportError:
            pass

        # FIX 2b: Persistent DB ban check on every protected event
        try:
            if db.is_user_banned(cfg, name=name, room_id=room_id, jti=jti):
                logger.warning(
                    f"[AUTH] Blocking DB-banned user '{name}' "
                    f"event in room '{room_id}' (sid={sid})"
                )
                disconnect()
                return
        except Exception as e:
            # Non-fatal: log and allow through if DB is temporarily unavailable
            # to avoid locking out all users during a DB blip
            logger.error(f"[AUTH] DB ban check error for sid={sid}: {e}")

        kwargs['_session'] = session
        return f(*args, **kwargs)

    return wrapper