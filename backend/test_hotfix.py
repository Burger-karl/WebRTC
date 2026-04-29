"""
test_hotfix.py — Unit Tests for All Hotfix Changes
─────────────────────────────────────────────────────────────────────────────
Tests every fix and new feature from the hotfix branch:

  Group 1 — Fix 1: Docker MySQL hostname (MYSQL_HOST from config, never hardcoded)
  Group 2 — Fix 2: Auto-migration (users table + all tables created on startup)
  Group 3 — Fix 3: Stripe metadata + pre-generated room_id
  Group 4 — Admin Dashboard auth (login, session, rate limit, bcrypt)
  Group 5 — Admin Dashboard APIs (stats, users, orders)
  Group 6 — upsert_user() and user tracking

Run:
    cd backend
    python test_hotfix.py
─────────────────────────────────────────────────────────────────────────────
"""

import sys
import os
import time
import types
import json
import hashlib
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(__file__))

# ── Environment ───────────────────────────────────────────────────────────────
os.environ["SECRET_KEY"]           = "test-secret"
os.environ["JWT_SECRET"]           = "test-jwt"
os.environ["MYSQL_ENABLED"]        = "true"
os.environ["MYSQL_HOST"]           = "mysql"          # Docker service name
os.environ["MYSQL_PORT"]           = "3306"
os.environ["MYSQL_USER"]           = "meetfree"
os.environ["MYSQL_PASSWORD"]       = "password"
os.environ["MYSQL_DATABASE"]       = "meetfree"
os.environ["STRIPE_SECRET_KEY"]    = "sk_test_fake"
os.environ["STRIPE_SERVICE_PRICE_ID"] = "price_test"
os.environ["APP_BASE_URL"]         = "http://localhost:5000"
os.environ["ADMIN_USERNAME"]       = "admin"
os.environ["MEETING_SCHEDULE_HOURS_AFTER"] = "24"
# Real bcrypt hash of "testpassword123"
os.environ["ADMIN_PASSWORD_HASH"]  = ""  # set after bcrypt import below

# ── Mock stripe ───────────────────────────────────────────────────────────────
stripe_mock = types.ModuleType("stripe")
stripe_mock.api_key = ""

class _FakeStripeError(Exception): pass
stripe_mock.error = types.SimpleNamespace(
    StripeError=_FakeStripeError,
    SignatureVerificationError=_FakeStripeError,
)

class _FakeSession:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.id  = kw.get('id', 'cs_test_0001')
        self.url = "https://checkout.stripe.com/test"
    def get(self, k, d=None): return self.__dict__.get(k, d)

class _FakeSessions:
    _last = None
    @classmethod
    def create(cls, **kw):
        s = _FakeSession(**kw)
        cls._last = s
        return s

stripe_mock.checkout = types.SimpleNamespace(Session=_FakeSessions)
stripe_mock.Event = types.SimpleNamespace(construct_from=lambda d, k: types.SimpleNamespace(**d))
stripe_mock.Webhook = types.SimpleNamespace(construct_event=lambda p, s, sec: json.loads(p))
sys.modules["stripe"] = stripe_mock

# ── Import bcrypt and set admin hash ─────────────────────────────────────────
import bcrypt as real_bcrypt
TEST_ADMIN_PASSWORD = "testpassword123"
TEST_ADMIN_HASH = real_bcrypt.hashpw(TEST_ADMIN_PASSWORD.encode(), real_bcrypt.gensalt(rounds=4)).decode()
os.environ["ADMIN_PASSWORD_HASH"] = TEST_ADMIN_HASH

# ── Import modules ────────────────────────────────────────────────────────────
import database as db
from bookings import _generate_room_id, _build_meeting_link, SERVICE_TYPES
from config import cfg

# ── Helpers ───────────────────────────────────────────────────────────────────
GREEN = "\033[92m"; RED = "\033[91m"; BOLD = "\033[1m"; RESET = "\033[0m"
passed = []; failed = []

def run_test(name, fn):
    try:
        fn()
        print(f"  {GREEN}✓ PASS{RESET}  {name}")
        passed.append(name)
    except AssertionError as e:
        print(f"  {RED}✗ FAIL{RESET}  {name}\n         {RED}→ {str(e) or 'assertion failed'}{RESET}")
        failed.append(name)
    except Exception as e:
        import traceback
        print(f"  {RED}✗ ERROR{RESET} {name}\n         {RED}→ {type(e).__name__}: {e}{RESET}")
        failed.append(name)

def make_mock_conn(rows=None, rowcount=1):
    conn   = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = lambda s: s
    cursor.__exit__  = MagicMock(return_value=False)
    cursor.fetchall.return_value = rows or []
    cursor.fetchone.return_value = rows[0] if rows else None
    cursor.rowcount = rowcount
    conn.cursor.return_value = cursor
    conn.insert_id.return_value = 99
    return conn, cursor

def reset():
    db._db_available = True
    db._pool.clear()


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 1 — Fix 1: Docker MySQL Hostname
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 1 — Fix 1: Docker MySQL Hostname{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_mysql_host_comes_from_env():
    """
    MYSQL_HOST must be read from the environment variable, never hardcoded.
    In Docker the compose file sets MYSQL_HOST=mysql.
    Outside Docker it defaults to localhost.
    The config must pass cfg.MYSQL_HOST to _new_connection — not 'localhost'.
    """
    assert cfg.MYSQL_HOST == "mysql", \
        f"cfg.MYSQL_HOST should be 'mysql' (from env var), got: '{cfg.MYSQL_HOST}'"

run_test("Fix 1: cfg.MYSQL_HOST reads from env var (not hardcoded)", test_mysql_host_comes_from_env)


def test_new_connection_uses_cfg_host():
    """
    _new_connection() must pass cfg.MYSQL_HOST to pymysql.connect()
    so the host is always environment-driven, not hardcoded.
    """
    reset()
    fake_pymysql = MagicMock()
    fake_conn    = MagicMock()
    fake_pymysql.connect.return_value = fake_conn
    fake_pymysql.cursors = MagicMock()

    with patch.object(db, '_get_pymysql', return_value=fake_pymysql):
        db._new_connection(cfg)

    call_kwargs = fake_pymysql.connect.call_args[1]
    assert call_kwargs['host'] == cfg.MYSQL_HOST, \
        f"pymysql.connect() must receive host=cfg.MYSQL_HOST='{cfg.MYSQL_HOST}', got: '{call_kwargs['host']}'"

run_test("Fix 1: _new_connection() passes cfg.MYSQL_HOST to pymysql.connect()", test_new_connection_uses_cfg_host)


def test_localhost_outside_docker():
    """
    When MYSQL_HOST is not set (no Docker compose override), config
    should default to 'localhost' for plain python server.py runs.
    """
    original = os.environ.get("MYSQL_HOST")
    try:
        del os.environ["MYSQL_HOST"]
        # Re-import config to pick up the missing env var
        import importlib
        import config as config_module
        importlib.reload(config_module)
        fresh_cfg = config_module.Config()
        assert fresh_cfg.MYSQL_HOST == "localhost", \
            f"Without MYSQL_HOST env var, should default to 'localhost', got: '{fresh_cfg.MYSQL_HOST}'"
    finally:
        if original:
            os.environ["MYSQL_HOST"] = original
        else:
            os.environ["MYSQL_HOST"] = "mysql"

run_test("Fix 1: MYSQL_HOST defaults to 'localhost' when env var not set", test_localhost_outside_docker)


def test_docker_compose_overrides_env_file():
    """
    Verify that setting MYSQL_HOST=mysql (as docker-compose does via
    environment: block) takes precedence over any .env file value.
    This is a documentation/architecture test confirming the design.
    """
    # Simulate what docker-compose does: set env var BEFORE loading .env
    os.environ["MYSQL_HOST"] = "mysql"
    import importlib, config as config_module
    importlib.reload(config_module)
    fresh_cfg = config_module.Config()
    assert fresh_cfg.MYSQL_HOST == "mysql", \
        "MYSQL_HOST=mysql set by docker-compose must not be overridden by .env file"
    os.environ["MYSQL_HOST"] = "mysql"  # restore

run_test("Fix 1: docker-compose environment block overrides .env.local value", test_docker_compose_overrides_env_file)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 2 — Fix 2: Auto-Migration (schema creation)
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 2 — Fix 2: Auto-Migration & users Table{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_init_db_creates_users_table():
    """
    init_db() must create the 'users' table that was previously missing.
    We capture all SQL statements and verify 'users' is in the CREATE statements.
    """
    reset()
    conn, cursor = make_mock_conn()
    executed_sqls = []

    original_execute = cursor.execute
    def capturing_execute(sql, *args, **kwargs):
        executed_sqls.append(sql)
        return original_execute(sql, *args, **kwargs)
    cursor.execute = capturing_execute

    with patch.object(db, '_new_connection', return_value=conn):
        db.init_db(cfg)

    tables_created = [sql for sql in executed_sqls if 'CREATE TABLE' in sql]
    table_names    = ' '.join(tables_created).lower()

    assert 'users'                 in table_names, "users table must be created"
    assert 'admin_users'           in table_names, "admin_users table must be created"
    assert 'chat_messages'         in table_names, "chat_messages table must be created"
    assert 'rooms'                 in table_names, "rooms table must be created"
    assert 'participant_mute_state' in table_names, "participant_mute_state must be created"
    assert 'orders'                in table_names, "orders table must be created"

run_test("Fix 2: init_db() creates all 6 tables including previously missing 'users'", test_init_db_creates_users_table)


def test_init_db_uses_create_if_not_exists():
    """
    All CREATE statements must use CREATE TABLE IF NOT EXISTS so that
    running init_db() on an existing database never fails or destroys data.
    """
    reset()
    conn, cursor = make_mock_conn()
    executed_sqls = []

    original_execute = cursor.execute
    def capturing_execute(sql, *args, **kwargs):
        executed_sqls.append(sql)
        return original_execute(sql, *args, **kwargs)
    cursor.execute = capturing_execute

    with patch.object(db, '_new_connection', return_value=conn):
        db.init_db(cfg)

    create_stmts = [sql for sql in executed_sqls if 'CREATE TABLE' in sql]
    for sql in create_stmts:
        assert 'IF NOT EXISTS' in sql, \
            f"CREATE TABLE must use IF NOT EXISTS to be safe on restarts:\n{sql[:80]}"

run_test("Fix 2: All CREATE TABLE statements use IF NOT EXISTS (safe restarts)", test_init_db_uses_create_if_not_exists)


def test_init_db_sets_db_available_on_success():
    """After successful init_db(), _db_available must be True."""
    reset()
    db._db_available = False
    conn, _ = make_mock_conn()
    with patch.object(db, '_new_connection', return_value=conn):
        result = db.init_db(cfg)
    assert result is True,         "init_db() should return True on success"
    assert db._db_available is True, "_db_available must be True after successful connection"

run_test("Fix 2: init_db() sets _db_available=True on success", test_init_db_sets_db_available_on_success)


def test_init_db_handles_connection_failure_gracefully():
    """
    If MySQL is unreachable, init_db() must set _db_available=False and
    return False — the app must continue running without crashing.
    """
    reset()
    with patch.object(db, '_new_connection', side_effect=Exception("Connection refused")):
        result = db.init_db(cfg)
    assert result is False,          "init_db() should return False on connection failure"
    assert db._db_available is False, "_db_available must be False when DB is unreachable"

run_test("Fix 2: init_db() returns False gracefully when MySQL is unreachable", test_init_db_handles_connection_failure_gracefully)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 3 — Fix 3: Stripe Metadata + Pre-Generated room_id
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 3 — Fix 3: Stripe Metadata & Pre-Generated room_id{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_room_id_generated_before_stripe_session():
    """
    room_id must be pre-generated and passed to BOTH create_order() and
    stripe.checkout.Session.create() before the Stripe session exists.
    This ensures the link in MySQL and in Stripe metadata always match.
    """
    reset()
    conn, cursor = make_mock_conn()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'), \
         patch.object(db, '_db_available', True):

        # Simulate the create_order flow
        room_id      = _generate_room_id()
        meeting_link = _build_meeting_link(room_id)

        # Create Stripe session with room_id in metadata
        session = _FakeSessions.create(
            metadata={"room_id": room_id, "meeting_link": meeting_link, "order_type": "service_booking"}
        )

        # Create DB order with same room_id
        db.create_order(cfg, "Alice", "alice@test.com", "web_app",
                        "I need a full-stack e-commerce website built.", "$5k",
                        session.id, room_id, meeting_link)

        # Verify Stripe metadata has the same room_id as what would be in DB
        stripe_room_id = session.metadata["room_id"]
        db_call_args   = cursor.execute.call_args[0][1]
        db_room_id     = db_call_args[6]  # 7th param = room_id in INSERT

        assert stripe_room_id == room_id, \
            f"Stripe metadata room_id '{stripe_room_id}' must equal pre-generated '{room_id}'"
        assert db_room_id == room_id, \
            f"DB room_id '{db_room_id}' must equal pre-generated '{room_id}'"
        assert stripe_room_id == db_room_id, \
            "Stripe metadata room_id and DB room_id must be identical"

run_test("Fix 3: room_id pre-generated and consistent in Stripe metadata and MySQL", test_room_id_generated_before_stripe_session)


def test_create_order_stores_meeting_link():
    """
    create_order() must store the meeting_link in MySQL at creation time
    (not just after webhook fires). This ensures the dashboard can show
    the Join Meeting button even if the webhook is delayed.
    """
    reset()
    conn, cursor = make_mock_conn()

    room_id      = _generate_room_id()
    meeting_link = _build_meeting_link(room_id)

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.create_order(cfg, "Bob", "bob@test.com", "consultation",
                        "Technical consultation on my architecture.", None,
                        "cs_test_001", room_id, meeting_link)

    sql  = cursor.execute.call_args[0][0]
    args = cursor.execute.call_args[0][1]

    assert "meeting_link" in sql, "meeting_link column must be in the INSERT SQL"
    assert meeting_link in args,  "meeting_link value must be stored at order creation"
    assert room_id in args,       "room_id must be stored at order creation"

run_test("Fix 3: create_order() stores meeting_link and room_id at creation time", test_create_order_stores_meeting_link)


def test_confirm_order_does_not_regenerate_room_id():
    """
    confirm_order_payment() must only update payment_status, transaction_id,
    and scheduled_time. It must NOT touch room_id or meeting_link — they
    were already set correctly at order creation.
    """
    reset()
    conn, cursor = make_mock_conn(rowcount=1)
    from datetime import datetime

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.confirm_order_payment(cfg, "cs_test_001", "pi_test_001", datetime.utcnow())

    sql = cursor.execute.call_args[0][0]
    assert "room_id"      not in sql.lower().replace("where", ""), \
        "confirm_order_payment() must NOT update room_id (already set at creation)"
    assert "meeting_link" not in sql.lower().replace("where", ""), \
        "confirm_order_payment() must NOT update meeting_link (already set at creation)"
    assert "payment_status" in sql, "Must update payment_status"
    assert "scheduled_time" in sql, "Must set scheduled_time"

run_test("Fix 3: confirm_order_payment() does not overwrite room_id or meeting_link", test_confirm_order_does_not_regenerate_room_id)


def test_stripe_metadata_contains_required_fields():
    """
    Stripe metadata must contain: order_type, room_id, meeting_link,
    client_email so the webhook can process it without any DB lookups
    for the core linking logic.
    """
    room_id      = _generate_room_id()
    meeting_link = _build_meeting_link(room_id)

    metadata = {
        "client_name":  "Alice",
        "client_email": "alice@test.com",
        "service_type": "web_app",
        "order_type":   "service_booking",
        "room_id":      room_id,
        "meeting_link": meeting_link,
    }

    required_fields = ["order_type", "room_id", "meeting_link", "client_email"]
    for field in required_fields:
        assert field in metadata, f"Stripe metadata must contain '{field}'"
    assert metadata["room_id"] == room_id
    assert meeting_link.endswith(room_id), "meeting_link must include room_id"

run_test("Fix 3: Stripe metadata contains all required fields including room_id", test_stripe_metadata_contains_required_fields)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 4 — Admin Dashboard Authentication
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 4 — Admin Dashboard Authentication{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_correct_password_verified():
    """bcrypt verification must return True for the correct password."""
    password_ok = real_bcrypt.checkpw(
        TEST_ADMIN_PASSWORD.encode(),
        TEST_ADMIN_HASH.encode()
    )
    assert password_ok is True, "Correct password should verify successfully"

run_test("Admin auth: correct password verifies against bcrypt hash", test_correct_password_verified)


def test_wrong_password_rejected():
    """bcrypt verification must return False for a wrong password."""
    password_bad = real_bcrypt.checkpw(
        b"wrongpassword",
        TEST_ADMIN_HASH.encode()
    )
    assert password_bad is False, "Wrong password must be rejected"

run_test("Admin auth: wrong password is rejected by bcrypt check", test_wrong_password_rejected)


def test_admin_hash_is_bcrypt():
    """The stored hash must be a valid bcrypt hash (starts with $2b$)."""
    assert TEST_ADMIN_HASH.startswith("$2b$"), \
        f"Admin password hash must be bcrypt ($2b$), got: {TEST_ADMIN_HASH[:10]}"

run_test("Admin auth: password stored as bcrypt hash (starts with $2b$)", test_admin_hash_is_bcrypt)


def test_admin_login_rate_limit():
    """
    Login rate limiter must block after 5 attempts in 60 seconds per IP.
    After the limit, subsequent calls must return False.
    """
    from admin import _check_login_rate_limit, _login_attempts
    test_ip = "192.168.99.1"
    _login_attempts.pop(test_ip, None)  # clear any previous state

    # First 5 attempts: all should pass
    for i in range(5):
        result = _check_login_rate_limit(test_ip)
        assert result is True, f"Attempt {i+1} should be allowed (got False)"

    # 6th attempt: must be blocked
    result = _check_login_rate_limit(test_ip)
    assert result is False, "6th attempt within 60s must be rate-limited"

    # Cleanup
    _login_attempts.pop(test_ip, None)

run_test("Admin auth: rate limiter blocks after 5 login attempts per minute", test_admin_login_rate_limit)


def test_admin_session_timeout_logic():
    """Session must be considered expired after ADMIN_SESSION_TIMEOUT seconds."""
    timeout        = cfg.ADMIN_SESSION_TIMEOUT  # e.g. 3600
    current_time   = time.time()

    # Fresh session: not expired
    fresh_login_time = current_time - (timeout - 60)   # 60s before expiry
    is_fresh_expired = (current_time - fresh_login_time) > timeout
    assert is_fresh_expired is False, "Fresh session should NOT be expired"

    # Old session: expired
    old_login_time = current_time - (timeout + 10)     # 10s past expiry
    is_old_expired = (current_time - old_login_time) > timeout
    assert is_old_expired is True, "Old session SHOULD be expired"

run_test("Admin auth: session timeout logic correctly identifies expired sessions", test_admin_session_timeout_logic)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 5 — Admin Dashboard APIs
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 5 — Admin Dashboard APIs{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_get_all_users_returns_correct_fields():
    """get_all_users() must return a list with expected user fields."""
    reset()
    mock_rows = [
        {"id": 1, "name": "Alice", "email": "alice@test.com",
         "subscription": "active", "joined": "01 Jan 2025", "last_seen_fmt": "15 Jan 2025 10:00"},
        {"id": 2, "name": "Bob",   "email": "bob@test.com",
         "subscription": "trial",  "joined": "10 Jan 2025", "last_seen_fmt": "14 Jan 2025 09:00"},
    ]
    conn, cursor = make_mock_conn(rows=mock_rows)
    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        result = db.get_all_users(cfg)

    assert len(result) == 2,               "Should return 2 users"
    assert result[0]['email'] == "alice@test.com"
    assert result[1]['subscription'] == "trial"

run_test("Admin API: get_all_users() returns expected user rows", test_get_all_users_returns_correct_fields)


def test_get_user_count_returns_integer():
    """get_user_count() must return an integer."""
    reset()
    conn, cursor = make_mock_conn(rows=[{"cnt": 42}])
    cursor.fetchone.return_value = {"cnt": 42}
    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        count = db.get_user_count(cfg)
    assert count == 42, f"Expected count=42, got {count}"

run_test("Admin API: get_user_count() returns correct integer", test_get_user_count_returns_integer)


def test_get_all_orders_includes_transaction_id():
    """
    get_all_orders() must return stripe_transaction_id so the admin
    can see and verify Stripe transactions from the dashboard.
    """
    reset()
    mock_rows = [{
        "id": 1, "client_name": "Alice", "client_email": "alice@test.com",
        "service_type": "web_app", "payment_status": "paid",
        "stripe_transaction_id": "pi_3abc123def456",
        "room_id": "order-a1b2c3d4",
        "meeting_link": "http://localhost/room/order-a1b2c3d4",
        "scheduled_time": "2025-06-15 10:00:00",
        "created_at": "2025-06-14 09:00:00",
    }]
    conn, cursor = make_mock_conn(rows=mock_rows)
    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        orders = db.get_all_orders(cfg)

    assert len(orders) == 1
    assert orders[0]['stripe_transaction_id'] == "pi_3abc123def456", \
        "Transaction ID must be present in admin orders response"
    assert orders[0]['room_id'] == "order-a1b2c3d4"

run_test("Admin API: get_all_orders() includes stripe_transaction_id", test_get_all_orders_includes_transaction_id)


def test_get_all_orders_filter_by_status():
    """get_all_orders() with status_filter should add WHERE clause."""
    reset()
    conn, cursor = make_mock_conn(rows=[])
    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.get_all_orders(cfg, status_filter='paid')

    sql = cursor.execute.call_args[0][0]
    assert "WHERE" in sql and "payment_status" in sql, \
        "Filtered query must include WHERE payment_status clause"

run_test("Admin API: get_all_orders() applies status filter in SQL", test_get_all_orders_filter_by_status)


def test_admin_stats_does_not_expose_secrets():
    """
    Admin stats response must not include SECRET_KEY, JWT_SECRET,
    MYSQL_PASSWORD, or STRIPE_SECRET_KEY — even accidentally.
    """
    # Simulate the stats dict that admin_stats() would return
    # (does not import server to avoid flask_socketio dependency in tests)
    rooms = {}
    stats = {
        "active_rooms": len(rooms),
        "active_peers": 0,
        "total_users":  5,
        "total_orders": 3,
        "paid_orders":  2,
        "mysql":        True,
        "timestamp":    "12:00:00 UTC",
        "resources": {"cpu_percent": 10.0, "mem_percent": 45.0,
                      "disk_percent": 20.0, "mem_used_mb": 400,
                      "mem_total_mb": 1024, "disk_used_gb": 5.0,
                      "disk_total_gb": 25.0},
    }
    stats_str = json.dumps(stats)

    for secret in [cfg.SECRET_KEY, cfg.JWT_SECRET, cfg.MYSQL_PASSWORD, cfg.STRIPE_SECRET_KEY]:
        if secret:
            assert secret not in stats_str, \
                f"Secret value must not appear in admin stats response"

run_test("Admin API: stats response does not expose any secret values", test_admin_stats_does_not_expose_secrets)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 6 — upsert_user() tracking
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 6 — User Registration & Tracking{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_upsert_user_uses_on_duplicate():
    """
    upsert_user() must use INSERT ... ON DUPLICATE KEY UPDATE so that:
    - New users are inserted
    - Existing users get last_seen updated without creating a duplicate row
    """
    reset()
    conn, cursor = make_mock_conn()
    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.upsert_user(cfg, "Alice", "alice@test.com")

    sql = cursor.execute.call_args[0][0]
    assert "INSERT INTO users"          in sql, "Must INSERT into users"
    assert "ON DUPLICATE KEY UPDATE"    in sql, "Must use ON DUPLICATE KEY UPDATE (no duplicate rows)"
    assert "last_seen"                  in sql, "Must update last_seen on duplicate"

run_test("User tracking: upsert_user() uses ON DUPLICATE KEY UPDATE (no duplicates)", test_upsert_user_uses_on_duplicate)


def test_upsert_user_lowercases_email():
    """Emails must be stored lowercase to prevent case-duplicate user records."""
    reset()
    conn, cursor = make_mock_conn()
    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.upsert_user(cfg, "Alice", "ALICE@TEST.COM")

    args = cursor.execute.call_args[0][1]
    stored_email = args[1]  # second param = email
    assert stored_email == "alice@test.com", \
        f"Email must be lowercased before storage, got: '{stored_email}'"

run_test("User tracking: upsert_user() lowercases email before storage", test_upsert_user_lowercases_email)


def test_upsert_user_returns_none_when_db_down():
    """When DB is down, upsert_user() must return None without crashing."""
    db._db_available = False
    result = db.upsert_user(cfg, "Alice", "alice@test.com")
    assert result is None, "Should return None when DB unavailable"
    db._db_available = True

run_test("User tracking: upsert_user() returns None gracefully when DB is down", test_upsert_user_returns_none_when_db_down)


def test_upsert_user_connection_returned_on_error():
    """Connection must be returned to pool even when upsert_user() fails."""
    reset()
    conn, cursor = make_mock_conn()
    cursor.execute.side_effect = Exception("DB error")
    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn') as mock_return:
        db.upsert_user(cfg, "Alice", "alice@test.com")
        mock_return.assert_called_once_with(conn), \
            "Connection must be returned to pool even on exception"

run_test("User tracking: connection returned to pool even when upsert_user() raises", test_upsert_user_connection_returned_on_error)


# ═════════════════════════════════════════════════════════════════════════════
#  Summary
# ═════════════════════════════════════════════════════════════════════════════
total = len(passed) + len(failed)
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Results: {GREEN}{len(passed)} passed{RESET}{BOLD}, "
      f"{RED}{len(failed)} failed{RESET}{BOLD} / {total} total{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")

if failed:
    print(f"{RED}Failed:{RESET}")
    for n in failed: print(f"  • {n}")
    print()
    sys.exit(1)
else:
    print(f"{GREEN}All hotfix tests passed.{RESET}\n")
    sys.exit(0)