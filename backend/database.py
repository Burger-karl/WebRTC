"""
database.py — MySQL Integration for MeetFree

Fixes in this version:
  1. Docker hostname fix: MYSQL_HOST is now read entirely from config (env var).
     The _new_connection() function always uses cfg.MYSQL_HOST — never a
     hardcoded string. In Docker, compose sets MYSQL_HOST=mysql. Outside
     Docker, it defaults to localhost. No code change needed here — the fix
     is ensuring config.py and docker-compose correctly pass the right value.

  2. Auto-migration: init_db() now creates ALL tables including the previously
     missing 'users' table and new 'admin_users' table. Uses CREATE TABLE IF
     NOT EXISTS so re-running never fails or destroys data.

  3. Stripe metadata fix: create_order() now accepts a pre-generated room_id
     so the room_id can be embedded in Stripe metadata BEFORE the checkout
     session is created. This means the webhook receives room_id directly
     in the metadata rather than generating it post-payment — ensuring the
     "Join Meeting" button always has the correct link.

Schema (all tables auto-created on startup):
  users                 — registered platform users
  admin_users           — admin dashboard credentials
  chat_messages         — persistent room chat history
  rooms                 — room records + bcrypt password hashes
  participant_mute_state— host mute state per participant
  orders                — booking orders + Stripe transaction links
"""

import logging
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger("meetfree.db")

_pymysql       = None
_bcrypt        = None
_pool: list    = []
_pool_size: int = 5
_db_available: bool = False


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
    Connect to MySQL and auto-create all tables if they don't exist.
    Called once at server startup from server.py.

    FIX 1: Uses cfg.MYSQL_HOST exclusively — never a hardcoded hostname.
    FIX 2: Creates the previously missing 'users' table plus 'admin_users'.
    All tables use CREATE TABLE IF NOT EXISTS — safe to run on every startup.
    """
    global _db_available

    if not cfg.MYSQL_ENABLED:
        logger.info("[DB] MySQL disabled (MYSQL_ENABLED=false)")
        return False

    try:
        conn = _new_connection(cfg)
        with conn.cursor() as cur:

            # ── users ─────────────────────────────────────────
            # Registered platform users (email, name, subscription status)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    name          VARCHAR(100)  NOT NULL,
                    email         VARCHAR(120)  NOT NULL UNIQUE,
                    password_hash VARCHAR(255)  DEFAULT NULL,
                    subscription  ENUM('trial','active','cancelled','expired')
                                  DEFAULT 'trial',
                    trial_ends_at DATETIME      DEFAULT NULL,
                    created_at    DATETIME      DEFAULT CURRENT_TIMESTAMP,
                    last_seen     DATETIME      DEFAULT NULL,
                    INDEX idx_email (email)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── admin_users ───────────────────────────────────
            # Admin dashboard login credentials
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_users (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    username      VARCHAR(50)   NOT NULL UNIQUE,
                    password_hash VARCHAR(255)  NOT NULL,
                    created_at    DATETIME      DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── chat_messages ─────────────────────────────────
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

            # ── rooms ─────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    room_id       VARCHAR(80)  PRIMARY KEY,
                    password_hash VARCHAR(255) DEFAULT NULL,
                    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                    created_by    VARCHAR(50)  NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── participant_mute_state ────────────────────────
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

            # ── orders ────────────────────────────────────────
            # FIX 3: room_id column stores the PRE-GENERATED room ID that
            # is also embedded in Stripe metadata before checkout is created.
            # This guarantees the webhook can use it directly instead of
            # generating a new one post-payment.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id                    INT AUTO_INCREMENT PRIMARY KEY,
                    client_name           VARCHAR(100) NOT NULL,
                    client_email          VARCHAR(120) NOT NULL,
                    service_type          VARCHAR(100) NOT NULL,
                    requirements          TEXT         NOT NULL,
                    budget                VARCHAR(50)  DEFAULT NULL,
                    stripe_session_id     VARCHAR(200) UNIQUE,
                    stripe_transaction_id VARCHAR(200) DEFAULT NULL,
                    payment_status        ENUM('pending','paid','failed','refunded')
                                          DEFAULT 'pending',
                    room_id               VARCHAR(80)  DEFAULT NULL,
                    meeting_link          VARCHAR(300) DEFAULT NULL,
                    scheduled_time        DATETIME     DEFAULT NULL,
                    created_at            DATETIME     DEFAULT CURRENT_TIMESTAMP,
                    updated_at            DATETIME     DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_email  (client_email),
                    INDEX idx_status (payment_status),
                    INDEX idx_room   (room_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

        conn.commit()
        conn.close()
        _db_available = True
        logger.info(
            f"[DB] MySQL connected at {cfg.MYSQL_HOST}:{cfg.MYSQL_PORT}"
            f"/{cfg.MYSQL_DATABASE} — all tables ready"
        )
        return True

    except Exception as e:
        logger.error(f"[DB] Connection failed: {e}")
        logger.warning("[DB] Running without database — DB features disabled")
        _db_available = False
        return False


def _new_connection(cfg):
    """
    Open a new PyMySQL connection.
    FIX 1: Always uses cfg.MYSQL_HOST — value comes from environment variable.
    In Docker: MYSQL_HOST=mysql (set by docker-compose environment: block).
    Outside Docker: MYSQL_HOST=localhost (default in config.py).
    """
    pymysql = _get_pymysql()
    return pymysql.connect(
        host            = cfg.MYSQL_HOST,      # never hardcoded
        port            = cfg.MYSQL_PORT,
        user            = cfg.MYSQL_USER,
        password        = cfg.MYSQL_PASSWORD,
        database        = cfg.MYSQL_DATABASE,
        charset         = 'utf8mb4',
        cursorclass     = pymysql.cursors.DictCursor,
        autocommit      = False,
        connect_timeout = 5,
    )


def _get_conn(cfg):
    """Get a pooled connection or open a fresh one."""
    if _pool:
        conn = _pool.pop()
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            pass
    return _new_connection(cfg)


def _return_conn(conn):
    """Return connection to pool; close it if pool is full."""
    if len(_pool) < _pool_size:
        _pool.append(conn)
    else:
        try:
            conn.close()
        except Exception:
            pass


# ── Users ─────────────────────────────────────────────────────────────────────

def upsert_user(cfg, name: str, email: str) -> Optional[int]:
    """
    Insert a new user or update last_seen for an existing one.
    Called when a user gets a JWT token (joins a room).
    Returns user ID or None on failure.
    """
    if not _db_available:
        return None
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (name, email, last_seen)
                VALUES (%s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    name      = VALUES(name),
                    last_seen = NOW()
            """, (name[:100], email[:120].lower()))
        conn.commit()
        return conn.insert_id() or None
    except Exception as e:
        logger.error(f"[DB] upsert_user error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return None
    finally:
        if conn:
            _return_conn(conn)


def get_all_users(cfg) -> list:
    """Admin: fetch all registered users ordered by most recent."""
    if not _db_available:
        return []
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, email, subscription,
                       DATE_FORMAT(created_at, '%d %b %Y') AS joined,
                       DATE_FORMAT(last_seen,  '%d %b %Y %H:%i') AS last_seen_fmt
                FROM users
                ORDER BY created_at DESC
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"[DB] get_all_users error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)


def get_user_count(cfg) -> int:
    """Return total registered user count."""
    if not _db_available:
        return 0
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM users")
            row = cur.fetchone()
            return row['cnt'] if row else 0
    except Exception as e:
        logger.error(f"[DB] get_user_count error: {e}")
        return 0
    finally:
        if conn:
            _return_conn(conn)


# ── Chat Persistence ──────────────────────────────────────────────────────────

def save_message(cfg, room_id: str, sender_name: str, message: str) -> Optional[int]:
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
        return conn.insert_id()
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
            return list(reversed(cur.fetchall()))
    except Exception as e:
        logger.error(f"[DB] get_room_history error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)


def delete_room_history(cfg, room_id: str) -> None:
    if not _db_available:
        return
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_messages WHERE room_id = %s", (room_id,))
            cur.execute("DELETE FROM participant_mute_state WHERE room_id = %s", (room_id,))
        conn.commit()
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
    if not _db_available:
        return True
    conn = None
    try:
        password_hash = None
        if password and password.strip():
            bcrypt = _get_bcrypt()
            password_hash = bcrypt.hashpw(
                password.encode('utf-8'), bcrypt.gensalt(rounds=12)
            ).decode('utf-8')
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT IGNORE INTO rooms (room_id, password_hash, created_by)
                VALUES (%s, %s, %s)
            """, (room_id, password_hash, created_by[:50]))
        conn.commit()
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
    return get_room(cfg, room_id) is not None


def verify_room_password(cfg, room_id: str, password: str) -> bool:
    if not _db_available:
        return True
    room = get_room(cfg, room_id)
    if not room:
        return True
    password_hash = room.get('password_hash')
    if not password_hash:
        return True
    try:
        bcrypt = _get_bcrypt()
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
    except Exception as e:
        logger.error(f"[DB] verify_room_password error: {e}")
        return False


def delete_room(cfg, room_id: str) -> None:
    if not _db_available:
        return
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rooms WHERE room_id = %s", (room_id,))
        conn.commit()
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
            return [r['peer_name'] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"[DB] get_muted_participants error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)


# ── Order Booking ─────────────────────────────────────────────────────────────

def create_order(cfg, client_name: str, client_email: str, service_type: str,
                 requirements: str, budget: Optional[str],
                 stripe_session_id: str,
                 room_id: str,
                 meeting_link: str) -> Optional[int]:
    """
    Create a pending order record.

    FIX 3: room_id and meeting_link are now passed in at creation time.
    They are pre-generated in bookings.py BEFORE the Stripe session is
    created, then embedded in Stripe metadata AND stored here simultaneously.
    This means the "Join Meeting" button link is consistent between
    the DB record, Stripe metadata, and the client dashboard.
    """
    if not _db_available:
        return None
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO orders
                    (client_name, client_email, service_type, requirements,
                     budget, stripe_session_id, payment_status, room_id, meeting_link)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
            """, (
                client_name[:100],
                client_email[:120].lower(),
                service_type[:100],
                requirements[:5000],
                budget[:50] if budget else None,
                stripe_session_id[:200],
                room_id[:80],
                meeting_link[:300],
            ))
        conn.commit()
        order_id = conn.insert_id()
        logger.info(f"[BOOKING] Order {order_id} created | room={room_id} | {client_email}")
        return order_id
    except Exception as e:
        logger.error(f"[DB] create_order error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return None
    finally:
        if conn:
            _return_conn(conn)


def confirm_order_payment(cfg, stripe_session_id: str,
                          stripe_transaction_id: str,
                          scheduled_time) -> bool:
    """
    Mark order as paid and set scheduled_time.
    FIX 3: room_id and meeting_link are already in the DB from create_order().
    We only need to update payment_status, transaction_id, and scheduled_time.
    """
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orders
                SET payment_status        = 'paid',
                    stripe_transaction_id = %s,
                    scheduled_time        = %s
                WHERE stripe_session_id = %s
                  AND payment_status    = 'pending'
            """, (
                stripe_transaction_id[:200],
                scheduled_time,
                stripe_session_id[:200],
            ))
            rows_updated = cur.rowcount
        conn.commit()
        if rows_updated == 0:
            logger.warning(f"[BOOKING] No pending order for session {stripe_session_id}")
            return False
        logger.info(f"[BOOKING] Order paid | session={stripe_session_id}")
        return True
    except Exception as e:
        logger.error(f"[DB] confirm_order_payment error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return False
    finally:
        if conn:
            _return_conn(conn)


def get_orders_by_email(cfg, client_email: str) -> list:
    if not _db_available:
        return []
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, client_name, service_type, requirements, budget,
                       payment_status, room_id, meeting_link,
                       DATE_FORMAT(scheduled_time, '%%W %%d %%M %%Y at %%H:%%i') AS scheduled_time_fmt,
                       DATE_FORMAT(created_at,     '%%d %%M %%Y')                AS created_at_fmt
                FROM orders
                WHERE client_email = %s
                ORDER BY created_at DESC
            """, (client_email.lower().strip(),))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"[DB] get_orders_by_email error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)


def get_order_by_session(cfg, stripe_session_id: str) -> Optional[dict]:
    if not _db_available:
        return None
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE stripe_session_id = %s", (stripe_session_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"[DB] get_order_by_session error: {e}")
        return None
    finally:
        if conn:
            _return_conn(conn)


def get_order_by_id(cfg, order_id: int) -> Optional[dict]:
    if not _db_available:
        return None
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"[DB] get_order_by_id error: {e}")
        return None
    finally:
        if conn:
            _return_conn(conn)


def get_all_orders(cfg, status_filter: str = None) -> list:
    """Admin: fetch all orders with full transaction details."""
    if not _db_available:
        return []
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            if status_filter:
                cur.execute("""
                    SELECT id, client_name, client_email, service_type,
                           payment_status, stripe_transaction_id,
                           room_id, meeting_link, scheduled_time, created_at
                    FROM orders
                    WHERE payment_status = %s
                    ORDER BY created_at DESC
                """, (status_filter,))
            else:
                cur.execute("""
                    SELECT id, client_name, client_email, service_type,
                           payment_status, stripe_transaction_id,
                           room_id, meeting_link, scheduled_time, created_at
                    FROM orders
                    ORDER BY created_at DESC
                """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"[DB] get_all_orders error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)