"""
database.py — MySQL Integration for MeetFree (feature-dev branch)
─────────────────────────────────────────────────────────────────────────────
Handles all database operations for the three new features:

  1. Persistent Chat
       - save_message()        — persist a chat message
       - get_room_history()    — fetch last N messages for a room
       - delete_room_history() — clean up when room is permanently closed

  2. Room Passwords
       - create_room()         — register a new room with optional password hash
       - get_room()            — fetch room record (to check password)
       - verify_room_password()— bcrypt comparison
       - room_exists()         — check if a room record is in DB
       - delete_room()         — remove room record

  3. Host Controls (DB-side: persisting mute state)
       - set_participant_muted()  — record mute state for a participant
       - get_muted_participants() — used when a late joiner enters (show who is muted)

Schema (auto-created on startup):

  chat_messages
    id           INT AUTO_INCREMENT PRIMARY KEY
    room_id      VARCHAR(80) NOT NULL
    sender_name  VARCHAR(50) NOT NULL
    message      TEXT NOT NULL
    sent_at      DATETIME DEFAULT CURRENT_TIMESTAMP
    INDEX idx_room_sent (room_id, sent_at)

  rooms
    room_id      VARCHAR(80) PRIMARY KEY
    password_hash VARCHAR(255)        — NULL = no password
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
    created_by   VARCHAR(50) NOT NULL

  participant_mute_state
    room_id      VARCHAR(80) NOT NULL
    peer_name    VARCHAR(50) NOT NULL
    is_muted     TINYINT(1) DEFAULT 0
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    PRIMARY KEY (room_id, peer_name)

Connection:
  Uses PyMySQL with a simple connection pool pattern. Falls back gracefully
  if MySQL is unavailable (chat and passwords are disabled; signaling still works).

Local testing without MySQL:
  Set MYSQL_ENABLED=false in .env — all DB functions become no-ops that
  return safe defaults. The app works fully without a database.
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger("meetfree.db")

# We import these lazily so the app starts even if PyMySQL is not installed
_pymysql = None
_bcrypt  = None

# Connection pool (simple list of connections)
_pool: list = []
_pool_size: int = 5
_db_available: bool = False   # set to True after successful init


def _get_pymysql():
    global _pymysql
    if _pymysql is None:
        import pymysql
        _pymysql = pymysql
    return _pymysql


def _get_bcrypt():
    global _bcrypt
    if _bcrypt is None:
        import bcrypt
        _bcrypt = bcrypt
    return _bcrypt


# ── Initialisation ────────────────────────────────────────────────────────────

def init_db(cfg) -> bool:
    """
    Connect to MySQL and create tables if they don't exist.
    Called once at server startup.

    Returns True if successful, False if MySQL is not available/configured.
    The server continues to run even if this returns False.
    """
    global _db_available

    if not cfg.MYSQL_ENABLED:
        logger.info("[DB] MySQL disabled via MYSQL_ENABLED=false — running without persistence")
        return False

    pymysql = _get_pymysql()

    try:
        conn = _new_connection(cfg)
        with conn.cursor() as cur:
            # ── chat_messages ─────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id          INT AUTO_INCREMENT PRIMARY KEY,
                    room_id     VARCHAR(80)  NOT NULL,
                    sender_name VARCHAR(50)  NOT NULL,
                    message     TEXT         NOT NULL,
                    sent_at     DATETIME     DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_room_sent (room_id, sent_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── rooms ─────────────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    room_id       VARCHAR(80)  PRIMARY KEY,
                    password_hash VARCHAR(255) DEFAULT NULL,
                    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                    created_by    VARCHAR(50)  NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── participant_mute_state ────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS participant_mute_state (
                    room_id    VARCHAR(80) NOT NULL,
                    peer_name  VARCHAR(50) NOT NULL,
                    is_muted   TINYINT(1)  DEFAULT 0,
                    updated_at DATETIME    DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (room_id, peer_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

        conn.commit()
        conn.close()
        _db_available = True
        logger.info(f"[DB] MySQL connected and schema ready at {cfg.MYSQL_HOST}:{cfg.MYSQL_PORT}/{cfg.MYSQL_DATABASE}")
        return True

    except Exception as e:
        logger.error(f"[DB] MySQL connection failed: {e}")
        logger.warning("[DB] Continuing without database — chat history, passwords, and mute persistence disabled")
        _db_available = False
        return False


def _new_connection(cfg):
    """Open a new PyMySQL connection using config values."""
    pymysql = _get_pymysql()
    return pymysql.connect(
        host     = cfg.MYSQL_HOST,
        port     = cfg.MYSQL_PORT,
        user     = cfg.MYSQL_USER,
        password = cfg.MYSQL_PASSWORD,
        database = cfg.MYSQL_DATABASE,
        charset  = 'utf8mb4',
        cursorclass = pymysql.cursors.DictCursor,
        autocommit  = False,
        connect_timeout = 5,
    )


def _get_conn(cfg):
    """
    Get a connection from the pool or open a new one.
    Simple round-robin pool — not thread-safe for production scale.
    For high-traffic production, replace with SQLAlchemy connection pool.
    """
    if _pool:
        conn = _pool.pop()
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            pass  # connection was stale, fall through to create new one
    return _new_connection(cfg)


def _return_conn(conn):
    """Return a connection to the pool."""
    if len(_pool) < _pool_size:
        _pool.append(conn)
    else:
        try:
            conn.close()
        except Exception:
            pass


# ── Chat Persistence ──────────────────────────────────────────────────────────

def save_message(cfg, room_id: str, sender_name: str, message: str) -> Optional[int]:
    """
    Persist a chat message to MySQL.
    Returns the new message ID, or None if DB is unavailable.

    Called from handle_chat() in server.py every time a message is sent.
    """
    if not _db_available:
        return None

    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_messages (room_id, sender_name, message) VALUES (%s, %s, %s)",
                (room_id, sender_name[:50], message[:500])
            )
        conn.commit()
        msg_id = conn.insert_id()
        logger.debug(f"[DB] Saved message id={msg_id} room='{room_id}' from '{sender_name}'")
        return msg_id
    except Exception as e:
        logger.error(f"[DB] save_message error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return None
    finally:
        if conn:
            _return_conn(conn)


def get_room_history(cfg, room_id: str, limit: int = 50) -> list:
    """
    Fetch the last `limit` messages for a room, oldest first.
    Returns a list of dicts: [{sender_name, message, sent_at}, ...]
    Returns [] if DB is unavailable or room has no history.

    Called when a new user joins a room so they see previous messages.
    """
    if not _db_available:
        return []

    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sender_name, message,
                       DATE_FORMAT(sent_at, '%%H:%%i') AS sent_at
                FROM chat_messages
                WHERE room_id = %s
                ORDER BY sent_at DESC
                LIMIT %s
            """, (room_id, limit))
            rows = cur.fetchall()
        # Reverse so oldest message is first in the returned list
        return list(reversed(rows))
    except Exception as e:
        logger.error(f"[DB] get_room_history error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)


def delete_room_history(cfg, room_id: str) -> None:
    """
    Delete all chat history for a room.
    Called when the last participant leaves (room cleanup).
    Optional — you may want to keep history; remove this call from
    handle_disconnect() in server.py if so.
    """
    if not _db_available:
        return

    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_messages WHERE room_id = %s", (room_id,))
            cur.execute("DELETE FROM participant_mute_state WHERE room_id = %s", (room_id,))
        conn.commit()
        logger.info(f"[DB] Deleted history and mute state for room '{room_id}'")
    except Exception as e:
        logger.error(f"[DB] delete_room_history error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
    finally:
        if conn:
            _return_conn(conn)


# ── Room Passwords ────────────────────────────────────────────────────────────

def create_room(cfg, room_id: str, created_by: str, password: Optional[str] = None) -> bool:
    """
    Register a new room in the database.
    If `password` is provided, it is bcrypt-hashed before storage.
    Returns True on success, False on failure.

    Called when the first user joins a room (they become host and set the password).
    """
    if not _db_available:
        return True   # no-op when DB is not available

    conn = None
    try:
        password_hash = None
        if password and password.strip():
            bcrypt = _get_bcrypt()
            # bcrypt.hashpw returns bytes — decode to str for MySQL storage
            password_hash = bcrypt.hashpw(
                password.encode('utf-8'),
                bcrypt.gensalt(rounds=12)
            ).decode('utf-8')

        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            # INSERT IGNORE: if room_id already exists, do nothing
            # This handles the edge case where the server restarts mid-session
            cur.execute("""
                INSERT IGNORE INTO rooms (room_id, password_hash, created_by)
                VALUES (%s, %s, %s)
            """, (room_id, password_hash, created_by[:50]))
        conn.commit()
        logger.info(f"[DB] Room '{room_id}' created by '{created_by}' | password={'set' if password_hash else 'none'}")
        return True
    except Exception as e:
        logger.error(f"[DB] create_room error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return False
    finally:
        if conn:
            _return_conn(conn)


def get_room(cfg, room_id: str) -> Optional[dict]:
    """
    Fetch a room record from the database.
    Returns dict with {room_id, password_hash, created_at, created_by}
    or None if the room doesn't exist in DB.
    """
    if not _db_available:
        return None

    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rooms WHERE room_id = %s", (room_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"[DB] get_room error: {e}")
        return None
    finally:
        if conn:
            _return_conn(conn)


def room_exists(cfg, room_id: str) -> bool:
    """Check if a room record exists in the database."""
    return get_room(cfg, room_id) is not None


def verify_room_password(cfg, room_id: str, password: str) -> bool:
    """
    Verify a submitted password against the stored bcrypt hash.

    Returns True if:
      - Room has no password (open room)
      - Password matches the stored hash

    Returns False if:
      - Room has a password and submitted password doesn't match
      - DB is unavailable (fail open — allow entry)
    """
    if not _db_available:
        return True   # fail open when DB not available

    room = get_room(cfg, room_id)
    if not room:
        return True   # room not in DB yet = no password set

    password_hash = room.get('password_hash')
    if not password_hash:
        return True   # room exists but has no password

    try:
        bcrypt = _get_bcrypt()
        return bcrypt.checkpw(
            password.encode('utf-8'),
            password_hash.encode('utf-8')
        )
    except Exception as e:
        logger.error(f"[DB] verify_room_password error: {e}")
        return False


def delete_room(cfg, room_id: str) -> None:
    """Remove a room record when it becomes permanently empty."""
    if not _db_available:
        return

    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rooms WHERE room_id = %s", (room_id,))
        conn.commit()
        logger.info(f"[DB] Room record deleted: '{room_id}'")
    except Exception as e:
        logger.error(f"[DB] delete_room error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
    finally:
        if conn:
            _return_conn(conn)


# ── Host Controls: Mute State ─────────────────────────────────────────────────

def set_participant_muted(cfg, room_id: str, peer_name: str, is_muted: bool) -> None:
    """
    Persist the mute state for a participant in a room.
    Used so late joiners can see who was muted by the host.
    """
    if not _db_available:
        return

    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO participant_mute_state (room_id, peer_name, is_muted)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE is_muted = %s, updated_at = CURRENT_TIMESTAMP
            """, (room_id, peer_name[:50], int(is_muted), int(is_muted)))
        conn.commit()
    except Exception as e:
        logger.error(f"[DB] set_participant_muted error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
    finally:
        if conn:
            _return_conn(conn)


def get_muted_participants(cfg, room_id: str) -> list:
    """
    Return a list of peer names who are currently muted by the host.
    Used when a new participant joins to show correct mute state.
    """
    if not _db_available:
        return []

    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT peer_name FROM participant_mute_state
                WHERE room_id = %s AND is_muted = 1
            """, (room_id,))
            rows = cur.fetchall()
        return [r['peer_name'] for r in rows]
    except Exception as e:
        logger.error(f"[DB] get_muted_participants error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)