"""
auth.py  -  JWT authentication for MeetFree signaling.

Flow:
  1. Client calls  POST /api/token  { "name": "Alice", "room": "my-room" }
  2. Server returns { "token": "<signed JWT>" }
  3. Client passes token when connecting Socket.IO:
       io({ query: { token } })
  4. authenticate_socket() validates the JWT on every new socket connection.
  5. @require_auth decorator guards every socket event handler.

JWT payload:
  {
    "sub":   "<display name>",
    "room":  "<room id>",          <- token is room-scoped
    "jti":   "<uuid4>",
    "iat":   <issued-at>,
    "exp":   <expiry>
  }
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

# Import request from flask - NOT from flask_socketio.
# Flask-SocketIO injects .sid and .environ into Flask's request context
# during socket events, so flask.request works correctly for both HTTP
# routes and socket handlers.
from flask import request
from flask_socketio import disconnect

from config import cfg

logger = logging.getLogger(__name__)


# -- Minimal HS256 JWT (no external library needed) ---------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def create_token(name: str, room: str) -> str:
    """
    Issue a signed JWT scoped to a specific room.
    Uses HMAC-SHA256 (HS256) - no external library required.
    """
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
    Raises ValueError on any failure (bad format, wrong sig, expired).
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

        # Constant-time comparison to prevent timing attacks
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

# Maps socket_id -> decoded JWT payload
# Set on connect, cleared on disconnect.
_socket_sessions: dict = {}


def authenticate_socket(sid: str, environ: dict) -> bool:
    """
    Validate the JWT for an incoming socket connection.
    Called from the 'connect' event handler in server.py.

    The client connects with:  io({ query: { token: '...' } })
    Flask-SocketIO puts the query string in environ['QUERY_STRING'].
    """
    from urllib.parse import parse_qs

    qs          = environ.get("QUERY_STRING", "")
    params      = parse_qs(qs)
    token_list  = params.get("token", [])

    if not token_list:
        logger.warning(f"[AUTH] Rejected - no token (sid={sid})")
        return False

    try:
        payload = decode_token(token_list[0])
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
    Checks the socket has a valid authenticated session.
    Injects _session=<JWT payload> into the handler's kwargs.

    Uses flask.request.sid - NOT flask_socketio.request.
    Flask-SocketIO sets request.sid on Flask's request context
    for the duration of each socket event, so this works correctly.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        sid     = request.sid
        session = get_session(sid)

        if not session:
            logger.warning(f"[AUTH] Unauthorised event from sid={sid} - disconnecting")
            disconnect()
            return

        kwargs['_session'] = session
        return f(*args, **kwargs)

    return wrapper