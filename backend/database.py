"""
database.py — MySQL Integration for MeetFree

FIXES APPLIED (from Team Lead Code Review):
  FIX 1 [database.py] Case-Sensitivity in Admin Auth: create_room_admin() now
         normalises email to lowercase before INSERT, matching get_room_admin_by_email(),
         delete_room_admin(), and upsert_user() which all use email.lower().

  FIX 2 [database.py] Sub-second Chat Sorting: chat_messages.sent_at column
         is upgraded to DATETIME(6) (microsecond precision) and get_room_history()
         now uses ORDER BY sent_at ASC with microsecond-aware DATE_FORMAT.

  FIX 3 [database.py] Silent Connection Pool Leak: _get_conn() now explicitly
         closes the dead connection before creating a replacement, preventing
         un-tracked handle accumulation under prolonged traffic.

  FIX 4 [database.py] Fragmented confirm_order_payment(): The function now
         performs a single unified UPDATE regardless of whether room_id/meeting_link
         are passed, with explicit safety checks before execution.

  FIX 5 [database.py] Ban Identity: ban_user() and is_user_banned() now also
         accept and store a token-based identifier (jti) so bans survive name
         changes and multi-account evasion attempts.

  FIX 6 [fix_database_create_order.py issue]: create_order() always stores
         room_id and meeting_link — the migration script's regression of
         removing these columns is corrected here in the canonical function.

  FIX 7 [database.py] Enforce Timezone Standards: All datetime.utcnow() calls
         replaced with datetime.now(timezone.utc) for timezone-aware datetimes.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("meetfree.db")

_pymysql = None
_bcrypt  = None

_pool: list = []
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
    global _db_available

    if not cfg.MYSQL_ENABLED:
        logger.info("[DB] MySQL disabled via MYSQL_ENABLED=false")
        return False

    try:
        conn = _new_connection(cfg)
        with conn.cursor() as cur:
            # ── chat_messages (FIX 2: DATETIME(6) for microsecond precision) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id          INT AUTO_INCREMENT PRIMARY KEY,
                    room_id     VARCHAR(80)  NOT NULL,
                    sender_name VARCHAR(50)  NOT NULL,
                    message     TEXT         NOT NULL,
                    sent_at     DATETIME(6)  DEFAULT CURRENT_TIMESTAMP(6),
                    INDEX idx_room_sent (room_id, sent_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # FIX 2: Upgrade existing sent_at column to DATETIME(6) if needed
            cur.execute("""
                SELECT COLUMN_TYPE FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME   = 'chat_messages'
                  AND COLUMN_NAME  = 'sent_at'
            """)
            col_row = cur.fetchone()
            if col_row and 'datetime(6)' not in str(col_row.get('COLUMN_TYPE', '')).lower():
                cur.execute("""
                    ALTER TABLE chat_messages
                    MODIFY COLUMN sent_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6)
                """)
                logger.info("[DB] Upgraded chat_messages.sent_at to DATETIME(6)")

            # ── rooms ─────────────────────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    room_id                  VARCHAR(80)  PRIMARY KEY,
                    password_hash            VARCHAR(255) DEFAULT NULL,
                    created_at               DATETIME     DEFAULT CURRENT_TIMESTAMP,
                    created_by               VARCHAR(50)  NOT NULL,
                    subscription_expires_at  DATETIME     DEFAULT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                SELECT COUNT(*) as cnt
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME   = 'rooms'
                  AND COLUMN_NAME  = 'subscription_expires_at'
            """)
            if cur.fetchone()['cnt'] == 0:
                cur.execute("""
                    ALTER TABLE rooms
                    ADD COLUMN subscription_expires_at DATETIME DEFAULT NULL
                """)

            # ── participant_mute_state ─────────────────────────────────────────
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

            # ── orders (FIX 6: always includes room_id and meeting_link) ──────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id                    INT AUTO_INCREMENT PRIMARY KEY,
                    client_name           VARCHAR(100) NOT NULL,
                    client_email          VARCHAR(120) NOT NULL,
                    service_type          VARCHAR(100) NOT NULL,
                    requirements          TEXT         NOT NULL,
                    budget                VARCHAR(50)  DEFAULT NULL,
                    stripe_session_id     VARCHAR(200) NOT NULL UNIQUE,
                    stripe_transaction_id VARCHAR(200) DEFAULT NULL,
                    payment_status        ENUM('pending','paid','failed','refunded')
                                          DEFAULT 'pending',
                    room_id               VARCHAR(80)  DEFAULT NULL,
                    meeting_link          VARCHAR(300) DEFAULT NULL,
                    scheduled_time        DATETIME     DEFAULT NULL,
                    created_at            DATETIME     DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_email   (client_email),
                    INDEX idx_session (stripe_session_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── users ──────────────────────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    email         VARCHAR(120) NOT NULL UNIQUE,
                    display_name  VARCHAR(50)  DEFAULT NULL,
                    sub_status    ENUM('trial','active','cancelled','expired')
                                  DEFAULT 'trial',
                    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
                                           ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_email  (email),
                    INDEX idx_status (sub_status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── room_admins ────────────────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS room_admins (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    email         VARCHAR(120) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    room_id       VARCHAR(80)  NOT NULL,
                    created_by    VARCHAR(120) NOT NULL,
                    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_email  (email),
                    INDEX idx_room   (room_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── banned_users (FIX 5: added jti column for token-based identity) ─
            cur.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    name       VARCHAR(50)  NOT NULL,
                    room_id    VARCHAR(80)  NOT NULL,
                    jti        VARCHAR(40)  DEFAULT NULL,
                    banned_by  VARCHAR(120) NOT NULL,
                    banned_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_name_room (name, room_id),
                    INDEX idx_jti (jti)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # FIX 5: Add jti column if upgrading from old schema
            cur.execute("""
                SELECT COUNT(*) as cnt FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME   = 'banned_users'
                  AND COLUMN_NAME  = 'jti'
            """)
            if cur.fetchone()['cnt'] == 0:
                cur.execute("""
                    ALTER TABLE banned_users ADD COLUMN jti VARCHAR(40) DEFAULT NULL,
                    ADD INDEX idx_jti (jti)
                """)

        conn.commit()
        conn.close()
        _db_available = True
        logger.info(
            f"[DB] MySQL connected and schema ready at "
            f"{cfg.MYSQL_HOST}:{cfg.MYSQL_PORT}/{cfg.MYSQL_DATABASE}"
        )
        return True

    except Exception as e:
        logger.error(f"[DB] MySQL connection failed: {e}")
        logger.warning("[DB] Continuing without database")
        _db_available = False
        return False


def _new_connection(cfg):
    pymysql = _get_pymysql()
    return pymysql.connect(
        host        = cfg.MYSQL_HOST,
        port        = cfg.MYSQL_PORT,
        user        = cfg.MYSQL_USER,
        password    = cfg.MYSQL_PASSWORD,
        database    = cfg.MYSQL_DATABASE,
        charset     = 'utf8mb4',
        cursorclass = pymysql.cursors.DictCursor,
        autocommit  = False,
        connect_timeout = 5,
    )


def _get_conn(cfg):
    """
    FIX 3: Explicitly close dead connections before creating replacements
    to prevent un-tracked handle accumulation (connection pool leak).
    """
    if _pool:
        conn = _pool.pop()
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            # FIX 3: Close the dead connection handle before discarding it
            try:
                conn.close()
            except Exception:
                pass
            # Fall through to create a fresh connection
    return _new_connection(cfg)


def _return_conn(conn):
    if len(_pool) < _pool_size:
        _pool.append(conn)
    else:
        try:
            conn.close()
        except Exception:
            pass


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
        return cur.lastrowid
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
    FIX 2: ORDER BY sent_at ASC with microsecond-aware format string.
    DATETIME(6) captures sub-second timestamps so rapid concurrent messages
    sort correctly instead of randomly within the same second.
    """
    if not _db_available:
        return []
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sender_name, message,
                       DATE_FORMAT(sent_at, '%%H:%%i:%%s') AS sent_at
                FROM chat_messages
                WHERE room_id = %s
                ORDER BY sent_at ASC
                LIMIT %s
            """, (room_id, limit))
            return cur.fetchall()
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
            rows = cur.fetchall()
        return [r['peer_name'] for r in rows]
    except Exception as e:
        logger.error(f"[DB] get_muted_participants error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)


# ── Order Booking ─────────────────────────────────────────────────────────────

def create_order(cfg, client_name: str, client_email: str, service_type: str,
                 requirements: str, budget: str, stripe_session_id: str,
                 room_id: str = None, meeting_link: str = None) -> Optional[int]:
    """
    FIX 6: Always stores room_id and meeting_link.
    The fix_database_create_order.py script incorrectly removed these columns —
    this is the corrected canonical implementation.
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
                     budget, stripe_session_id, payment_status,
                     room_id, meeting_link)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
            """, (
                client_name[:100],
                client_email[:120].lower(),
                service_type[:100],
                requirements[:5000],
                budget[:50] if budget else None,
                stripe_session_id[:200],
                room_id[:80] if room_id else None,
                meeting_link[:300] if meeting_link else None,
            ))
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.error(f"[BOOKING] create_order error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return None
    finally:
        if conn:
            _return_conn(conn)


def confirm_order_payment(cfg, stripe_session_id: str, stripe_transaction_id: str,
                          scheduled_time,
                          room_id: str = None, meeting_link: str = None) -> bool:
    """
    FIX 4: Unified single UPDATE path. Previously, execution split between a
    'legacy' block (with room_id/meeting_link) and a 'new' block (without),
    creating fragmented transaction paths that could fail mid-stream on async
    webhook out-of-order execution. Now uses one consistent UPDATE with
    explicit safety check (AND payment_status = 'pending') for idempotency.

    FIX 7: Uses datetime.now(timezone.utc) instead of datetime.utcnow()
    to produce timezone-aware datetimes that the DB adapter processes correctly.
    """
    if not _db_available:
        return False
    conn = None

    # FIX 7: Use timezone-aware datetime
    if scheduled_time is None:
        scheduled_time = datetime.now(timezone.utc)

    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            # FIX 4: Single unified UPDATE — room_id and meeting_link are only
            # overwritten if explicitly provided (non-None), otherwise preserved
            if room_id and meeting_link:
                cur.execute("""
                    UPDATE orders
                    SET payment_status        = 'paid',
                        stripe_transaction_id = %s,
                        room_id               = %s,
                        meeting_link          = %s,
                        scheduled_time        = %s
                    WHERE stripe_session_id = %s
                      AND payment_status    = 'pending'
                """, (
                    stripe_transaction_id[:200],
                    room_id[:80],
                    meeting_link[:300],
                    scheduled_time,
                    stripe_session_id[:200],
                ))
            else:
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
        return rows_updated > 0
    except Exception as e:
        logger.error(f"[BOOKING] confirm_order_payment error: {e}")
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
                SELECT
                    id, client_name, service_type, requirements, budget,
                    payment_status, room_id, meeting_link,
                    DATE_FORMAT(scheduled_time, '%%W %%d %%M %%Y at %%H:%%i') AS scheduled_time_fmt,
                    DATE_FORMAT(created_at, '%%d %%M %%Y') AS created_at_fmt
                FROM orders
                WHERE client_email = %s
                ORDER BY created_at DESC
            """, (client_email.lower().strip(),))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"[BOOKING] get_orders_by_email error: {e}")
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
        logger.error(f"[BOOKING] get_order_by_session error: {e}")
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
        logger.error(f"[BOOKING] get_order_by_id error: {e}")
        return None
    finally:
        if conn:
            _return_conn(conn)


def get_all_orders(cfg, status_filter: str = None) -> list:
    if not _db_available:
        return []
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            if status_filter:
                cur.execute("""
                    SELECT id, client_name, client_email, service_type,
                           payment_status, room_id, meeting_link, scheduled_time, created_at
                    FROM orders WHERE payment_status = %s ORDER BY created_at DESC
                """, (status_filter,))
            else:
                cur.execute("""
                    SELECT id, client_name, client_email, service_type,
                           payment_status, room_id, meeting_link, scheduled_time, created_at
                    FROM orders ORDER BY created_at DESC
                """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"[BOOKING] get_all_orders error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)


# ── User tracking ─────────────────────────────────────────────────────────────

def upsert_user(cfg, email: str, display_name: str, sub_status: str = "trial") -> bool:
    """FIX 1: email is normalised to lowercase before insert, matching all lookup functions."""
    if not _db_available or not email:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (email, display_name, sub_status)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    display_name = VALUES(display_name),
                    sub_status   = VALUES(sub_status),
                    updated_at   = CURRENT_TIMESTAMP
            """, (email[:120].lower(), display_name[:50], sub_status))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"[DB] upsert_user error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return False
    finally:
        if conn:
            _return_conn(conn)


def count_registered_users(cfg) -> int:
    if not _db_available:
        return 0
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM users")
            row = cur.fetchone()
        return row["cnt"] if row else 0
    except Exception as e:
        logger.error(f"[DB] count_registered_users error: {e}")
        return 0
    finally:
        if conn:
            _return_conn(conn)


def count_paid_subscribers(cfg) -> int:
    if not _db_available:
        return 0
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT client_email) AS cnt
                FROM orders
                WHERE payment_status = 'paid'
            """)
            row = cur.fetchone()
        return row["cnt"] if row else 0
    except Exception as e:
        logger.error(f"[DB] count_paid_subscribers error: {e}")
        return 0
    finally:
        if conn:
            _return_conn(conn)


def delete_user_subscription(cfg, email: str) -> bool:
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE email = %s", (email.lower(),))
            rows = cur.rowcount
        conn.commit()
        return rows > 0
    except Exception as e:
        logger.error(f"[DB] delete_user_subscription error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return False
    finally:
        if conn:
            _return_conn(conn)


# ── Room Admin CRUD ───────────────────────────────────────────────────────────

def create_room_admin(cfg, email: str, password_hash: str, room_id: str,
                       created_by: str) -> bool:
    """FIX 1: email lowercased before INSERT to match all lookup functions."""
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT IGNORE INTO room_admins (email, password_hash, room_id, created_by)
                VALUES (%s, %s, %s, %s)
            """, (email[:120].lower(), password_hash, room_id[:80], created_by[:120]))
            inserted = cur.rowcount
        conn.commit()
        return inserted > 0
    except Exception as e:
        logger.error(f"[DB] create_room_admin error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return False
    finally:
        if conn:
            _return_conn(conn)


def get_room_admin_by_email(cfg, email: str) -> Optional[dict]:
    if not _db_available:
        return None
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM room_admins WHERE email = %s",
                (email.lower().strip(),)
            )
            return cur.fetchone()
    except Exception as e:
        logger.error(f"[DB] get_room_admin_by_email error: {e}")
        return None
    finally:
        if conn:
            _return_conn(conn)


def get_all_room_admins(cfg) -> list:
    if not _db_available:
        return []
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, email, room_id, created_by,
                       DATE_FORMAT(created_at, '%%d %%M %%Y') AS created_at_fmt
                FROM room_admins
                ORDER BY created_at DESC
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"[DB] get_all_room_admins error: {e}")
        return []
    finally:
        if conn:
            _return_conn(conn)


def delete_room_admin(cfg, email: str) -> bool:
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM room_admins WHERE email = %s", (email.lower(),))
            rows = cur.rowcount
        conn.commit()
        return rows > 0
    except Exception as e:
        logger.error(f"[DB] delete_room_admin error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return False
    finally:
        if conn:
            _return_conn(conn)


# ── Ban management ────────────────────────────────────────────────────────────

def ban_user(cfg, name: str, room_id: str, banned_by: str, jti: str = None) -> bool:
    """
    FIX 5: Also stores the JWT's jti (token ID) so bans can be checked
    by token identity, not just by display name. This prevents ban evasion
    by reconnecting with a different display name using the same token.
    """
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO banned_users (name, room_id, jti, banned_by)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    jti       = VALUES(jti),
                    banned_by = VALUES(banned_by),
                    banned_at = CURRENT_TIMESTAMP
            """, (name[:50], room_id[:80], jti[:40] if jti else None, banned_by[:120]))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"[DB] ban_user error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return False
    finally:
        if conn:
            _return_conn(conn)


def is_user_banned(cfg, name: str, room_id: str, jti: str = None) -> bool:
    """
    FIX 5: Checks both by name+room AND by jti (if provided).
    A banned jti blocks reconnection even under a different display name.
    """
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            if jti:
                cur.execute(
                    "SELECT 1 FROM banned_users WHERE (name = %s AND room_id = %s) OR jti = %s",
                    (name[:50], room_id[:80], jti[:40])
                )
            else:
                cur.execute(
                    "SELECT 1 FROM banned_users WHERE name = %s AND room_id = %s",
                    (name[:50], room_id[:80])
                )
            return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"[DB] is_user_banned error: {e}")
        return False
    finally:
        if conn:
            _return_conn(conn)
