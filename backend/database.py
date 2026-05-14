"""
database.py — MySQL Integration for MeetFree (RBAC + Fixes branch)
─────────────────────────────────────────────────────────────────────────────
All existing functions are preserved and extended with:

  NEW TABLES:
    users              — registered users + subscription status
    room_admins        — Room Admin accounts (RBAC)
    banned_users       — users banned by Super Admin

  NEW FUNCTIONS:
    create_room_admin()        — Super Admin creates a Room Admin
    get_room_admin_by_email()  — authenticate a Room Admin
    get_all_room_admins()      — list all Room Admins
    delete_room_admin()        — Super Admin removes a Room Admin
    count_registered_users()   — global user count for stats
    count_paid_subscribers()   — global paid subscriber count
    upsert_user()              — register/update a user on token issue
    delete_user_subscription() — Super Admin deletes a user
    ban_user()                 — record a ban
    is_user_banned()           — check if a name+room is banned

  SCHEMA ADDITION to rooms table:
    subscription_expires_at — Room Admin sees their room expiry

─────────────────────────────────────────────────────────────────────────────
"""

import logging
import time
from datetime import datetime
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

            # ── rooms (extended with subscription_expires_at) ─────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rooms (
                    room_id                  VARCHAR(80)  PRIMARY KEY,
                    password_hash            VARCHAR(255) DEFAULT NULL,
                    created_at               DATETIME     DEFAULT CURRENT_TIMESTAMP,
                    created_by               VARCHAR(50)  NOT NULL,
                    subscription_expires_at  DATETIME     DEFAULT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # Add column if upgrading from old schema (MySQL 5.7 compatible)
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

            # ── orders ────────────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id                   INT AUTO_INCREMENT PRIMARY KEY,
                    client_name          VARCHAR(100) NOT NULL,
                    client_email         VARCHAR(120) NOT NULL,
                    service_type         VARCHAR(100) NOT NULL,
                    requirements         TEXT         NOT NULL,
                    budget               VARCHAR(50)  DEFAULT NULL,
                    stripe_session_id    VARCHAR(200) NOT NULL UNIQUE,
                    stripe_transaction_id VARCHAR(200) DEFAULT NULL,
                    payment_status       ENUM('pending','paid','failed','refunded')
                                         DEFAULT 'pending',
                    room_id              VARCHAR(80)  DEFAULT NULL,
                    meeting_link         VARCHAR(300) DEFAULT NULL,
                    scheduled_time       DATETIME     DEFAULT NULL,
                    created_at           DATETIME     DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_email (client_email),
                    INDEX idx_session (stripe_session_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── users (NEW) ───────────────────────────────────────────
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
                    INDEX idx_email (email),
                    INDEX idx_status (sub_status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # ── room_admins (NEW) ─────────────────────────────────────
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

            # ── banned_users (NEW) ────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    name       VARCHAR(50)  NOT NULL,
                    room_id    VARCHAR(80)  NOT NULL,
                    banned_by  VARCHAR(120) NOT NULL,
                    banned_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_name_room (name, room_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

        conn.commit()
        conn.close()
        _db_available = True
        logger.info(f"[DB] MySQL connected and schema ready at {cfg.MYSQL_HOST}:{cfg.MYSQL_PORT}/{cfg.MYSQL_DATABASE}")
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
    if _pool:
        conn = _pool.pop()
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            pass
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
        return list(reversed(rows))
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
    Create a pending order. room_id and meeting_link are pre-generated
    in bookings.py before the Stripe session is created, so they are
    stored immediately rather than waiting for the webhook.
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
    Mark an order as paid and set the scheduled_time.
    room_id and meeting_link are already stored from create_order(),
    so they only need updating if explicitly passed (legacy support).
    """
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            if room_id and meeting_link:
                # Legacy path: update room_id and meeting_link too
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
                # New path: room_id and meeting_link already set in create_order()
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


# ══════════════════════════════════════════════════════════════════════════════
# NEW: User tracking
# ══════════════════════════════════════════════════════════════════════════════

def upsert_user(cfg, email: str, display_name: str, sub_status: str = "trial") -> bool:
    """
    Register or update a user when they get a token.
    This is how 'total registered users' is tracked.
    """
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
    """Total number of users ever registered (for Super Admin stats)."""
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
    """Total users with active paid subscriptions (for Super Admin stats)."""
    if not _db_available:
        return 0
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            # Count orders with payment_status=paid as a proxy for paid subscribers
            # (used when the users.sub_status column may not exist yet)
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
    """Super Admin: remove a user's record entirely."""
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


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Room Admin CRUD
# ══════════════════════════════════════════════════════════════════════════════

def create_room_admin(cfg, email: str, password_hash: str, room_id: str,
                       created_by: str) -> bool:
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT IGNORE INTO room_admins (email, password_hash, room_id, created_by)
                VALUES (%s, %s, %s, %s)
            """, (email[:120], password_hash, room_id[:80], created_by[:120]))
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


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Ban management
# ══════════════════════════════════════════════════════════════════════════════

def ban_user(cfg, name: str, room_id: str, banned_by: str) -> bool:
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT IGNORE INTO banned_users (name, room_id, banned_by)
                VALUES (%s, %s, %s)
            """, (name[:50], room_id[:80], banned_by[:120]))
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


def is_user_banned(cfg, name: str, room_id: str) -> bool:
    if not _db_available:
        return False
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
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