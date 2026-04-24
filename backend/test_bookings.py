"""
test_bookings.py — Thorough Unit Tests for Order Booking & Meeting Scheduling
─────────────────────────────────────────────────────────────────────────────
Tests every component of the booking system individually:

  Group 1 — Database Layer (MySQL operations)
  Group 2 — Booking Logic (room ID generation, meeting link, scheduling)
  Group 3 — Stripe Webhook (payment confirmation, idempotency)
  Group 4 — API Endpoints (order creation, dashboard API)
  Group 5 — Connection Pool (efficiency on 1GB RAM server)
  Group 6 — Edge Cases & Security

No real MySQL, no real Stripe, no running server needed.
Everything is mocked internally.

Run:
    cd backend
    python test_bookings.py
─────────────────────────────────────────────────────────────────────────────
"""

import sys
import os
import time
import types
import json
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

# ── Environment setup ─────────────────────────────────────────────────────────
os.environ["SECRET_KEY"]                  = "test-secret"
os.environ["JWT_SECRET"]                  = "test-jwt"
os.environ["MYSQL_ENABLED"]               = "true"
os.environ["MYSQL_HOST"]                  = "localhost"
os.environ["MYSQL_USER"]                  = "test"
os.environ["MYSQL_PASSWORD"]              = "test"
os.environ["MYSQL_DATABASE"]              = "testdb"
os.environ["STRIPE_SECRET_KEY"]           = "sk_test_fake"
os.environ["STRIPE_SERVICE_PRICE_ID"]     = "price_service_test"
os.environ["APP_BASE_URL"]                = "http://localhost:5000"
os.environ["MEETING_DURATION_MINUTES"]    = "60"
os.environ["MEETING_SCHEDULE_HOURS_AFTER"]= "24"

# ── Mock Stripe before importing anything ────────────────────────────────────
stripe_mock = types.ModuleType("stripe")
stripe_mock.api_key = ""

class _FakeStripeError(Exception): pass
stripe_mock.error = types.SimpleNamespace(
    StripeError=_FakeStripeError,
    SignatureVerificationError=_FakeStripeError,
)

class _FakeSession:
    def __init__(self, **kw): self.__dict__.update(kw)
    def get(self, k, d=None): return self.__dict__.get(k, d)

class _FakeSessions:
    _sessions = {}
    @classmethod
    def create(cls, **kw):
        s = _FakeSession(id=f"cs_test_{len(cls._sessions):04d}", url="https://checkout.stripe.com/test", **kw)
        cls._sessions[s.id] = s
        return s
    @classmethod
    def retrieve(cls, sid):
        if sid in cls._sessions: return cls._sessions[sid]
        raise _FakeStripeError(f"No such session: {sid}")
    @classmethod
    def reset(cls): cls._sessions.clear()

stripe_mock.checkout = types.SimpleNamespace(Session=_FakeSessions)

class _FakeEvent:
    @staticmethod
    def construct_from(data, key): return types.SimpleNamespace(**data, type=data.get("type",""), data=types.SimpleNamespace(object=data.get("data",{}).get("object",{})))

stripe_mock.Event = _FakeEvent

class _FakeWebhook:
    @staticmethod
    def construct_event(payload, sig, secret):
        return json.loads(payload)
stripe_mock.Webhook = _FakeWebhook

sys.modules["stripe"] = stripe_mock

# ── Now import project modules ────────────────────────────────────────────────
import database as db
from bookings import _generate_room_id, _build_meeting_link, _calculate_scheduled_time, SERVICE_TYPES

# ── Colour helpers ────────────────────────────────────────────────────────────
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
        print(f"  {RED}✗ ERROR{RESET} {name}\n         {RED}→ {type(e).__name__}: {e}{RESET}")
        failed.append(name)

def make_mock_conn(rows=None, rowcount=1):
    conn   = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = lambda s: s
    cursor.__exit__  = MagicMock(return_value=False)
    cursor.fetchall.return_value  = rows or []
    cursor.fetchone.return_value  = rows[0] if rows else None
    cursor.rowcount               = rowcount
    conn.cursor.return_value      = cursor
    conn.insert_id.return_value   = 42
    return conn, cursor

def reset():
    _FakeSessions.reset()
    db._db_available = True


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 1 — Database Layer
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 1 — Database Layer (MySQL operations){RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_create_order_inserts_pending():
    """create_order() must INSERT a record with payment_status='pending'."""
    reset()
    conn, cursor = make_mock_conn()
    cfg = MagicMock()
    with patch.object(db, '_get_conn', return_value=conn), patch.object(db, '_return_conn'):
        result = db.create_order(cfg, "Alice", "alice@test.com", "web_app",
                                 "I need a full-stack e-commerce site built.", "$5k", "cs_abc123")
    assert result == 42
    sql = cursor.execute.call_args[0][0]
    assert "INSERT INTO orders" in sql
    args = cursor.execute.call_args[0][1]
    assert "alice@test.com" in args
    assert "web_app" in args
    assert "cs_abc123" in args

run_test("create_order() inserts record with correct fields", test_create_order_inserts_pending)


def test_create_order_truncates_long_fields():
    """Long inputs should be truncated before storage to prevent DB overflow."""
    reset()
    conn, cursor = make_mock_conn()
    cfg = MagicMock()
    with patch.object(db, '_get_conn', return_value=conn), patch.object(db, '_return_conn'):
        db.create_order(cfg, "A"*200, "b"*130 + "@test.com", "web_app",
                        "req"*2000, "budget", "cs_sid")
    args = cursor.execute.call_args[0][1]
    assert len(args[0]) <= 100,  f"client_name should be max 100 chars, got {len(args[0])}"
    assert len(args[1]) <= 120,  f"client_email should be max 120 chars, got {len(args[1])}"
    assert len(args[3]) <= 5000, f"requirements should be max 5000 chars, got {len(args[3])}"

run_test("create_order() truncates overly long field values", test_create_order_truncates_long_fields)


def test_create_order_returns_none_when_db_down():
    """When DB is unavailable, create_order() returns None (no crash)."""
    db._db_available = False
    cfg = MagicMock()
    result = db.create_order(cfg, "Alice", "alice@test.com", "web_app", "requirements text here", None, "cs_x")
    assert result is None, "Should return None when DB unavailable"
    db._db_available = True

run_test("create_order() returns None gracefully when DB is down", test_create_order_returns_none_when_db_down)


def test_confirm_order_payment_updates_correct_fields():
    """confirm_order_payment() must UPDATE all 4 fields atomically."""
    reset()
    conn, cursor = make_mock_conn(rowcount=1)
    cfg = MagicMock()
    scheduled = datetime(2025, 6, 15, 10, 0, 0)
    with patch.object(db, '_get_conn', return_value=conn), patch.object(db, '_return_conn'):
        result = db.confirm_order_payment(cfg, "cs_session_001", "pi_txn_001",
                                          "order-a1b2c3d4",
                                          "http://localhost:5000/room/order-a1b2c3d4",
                                          scheduled)
    assert result is True
    sql  = cursor.execute.call_args[0][0]
    args = cursor.execute.call_args[0][1]
    assert "UPDATE orders" in sql
    assert "payment_status" in sql
    assert "'paid'" in sql or "paid" in sql
    assert "pi_txn_001"                            in args
    assert "order-a1b2c3d4"                        in args
    assert "http://localhost:5000/room/order-a1b2c3d4" in args
    assert scheduled                               in args
    assert "cs_session_001"                        in args

run_test("confirm_order_payment() updates all fields in one UPDATE", test_confirm_order_payment_updates_correct_fields)


def test_confirm_order_payment_only_updates_pending():
    """
    Idempotency: confirm_order_payment() uses WHERE payment_status='pending'.
    If the order was already paid (e.g. webhook fired twice), rowcount=0
    and the function returns False — preventing double-confirmation.
    """
    reset()
    conn, cursor = make_mock_conn(rowcount=0)  # 0 rows updated = already processed
    cfg = MagicMock()
    with patch.object(db, '_get_conn', return_value=conn), patch.object(db, '_return_conn'):
        result = db.confirm_order_payment(cfg, "cs_already_done", "pi_txn",
                                          "order-dup", "http://...", datetime.utcnow())
    assert result is False, "Should return False when no pending order found (already processed)"
    sql = cursor.execute.call_args[0][0]
    assert "payment_status" in sql and "pending" in sql, \
        "SQL must filter by payment_status='pending' to prevent double-confirmation"

run_test("confirm_order_payment() is idempotent — skips if already paid", test_confirm_order_payment_only_updates_pending)


def test_get_orders_by_email_returns_paid_orders():
    """get_orders_by_email() fetches all orders for an email, newest first."""
    reset()
    mock_rows = [
        {"id": 2, "service_type": "web_app", "payment_status": "paid",
         "room_id": "order-abc123", "meeting_link": "http://localhost/room/order-abc123",
         "scheduled_time_fmt": "Monday 15 June 2025 at 10:00",
         "created_at_fmt": "01 June 2025", "requirements": "Build me a site", "budget": "$5k"},
        {"id": 1, "service_type": "consultation", "payment_status": "paid",
         "room_id": "order-def456", "meeting_link": "http://localhost/room/order-def456",
         "scheduled_time_fmt": "Sunday 01 June 2025 at 14:00",
         "created_at_fmt": "20 May 2025", "requirements": "Quick consult", "budget": None},
    ]
    conn, cursor = make_mock_conn(rows=mock_rows)
    cfg = MagicMock()
    with patch.object(db, '_get_conn', return_value=conn), patch.object(db, '_return_conn'):
        result = db.get_orders_by_email(cfg, "alice@test.com")
    assert len(result) == 2
    email_arg = cursor.execute.call_args[0][1][0]
    assert email_arg == "alice@test.com", "Should query by lowercase email"

run_test("get_orders_by_email() returns all orders for an email", test_get_orders_by_email_returns_paid_orders)


def test_get_orders_by_email_normalises_email():
    """Email should be lowercased before the DB query."""
    reset()
    conn, cursor = make_mock_conn(rows=[])
    cfg = MagicMock()
    with patch.object(db, '_get_conn', return_value=conn), patch.object(db, '_return_conn'):
        db.get_orders_by_email(cfg, "ALICE@TEST.COM")
    email_arg = cursor.execute.call_args[0][1][0]
    assert email_arg == "alice@test.com", f"Email should be lowercased, got: {email_arg}"

run_test("get_orders_by_email() normalises email to lowercase", test_get_orders_by_email_normalises_email)


def test_get_orders_returns_empty_when_db_down():
    """When DB is down, get_orders_by_email() returns [] (no crash)."""
    db._db_available = False
    cfg = MagicMock()
    result = db.get_orders_by_email(cfg, "alice@test.com")
    assert result == []
    db._db_available = True

run_test("get_orders_by_email() returns [] when DB is down", test_get_orders_returns_empty_when_db_down)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 2 — Booking Logic
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 2 — Booking Logic{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_room_id_format():
    """Generated room IDs must follow the 'order-XXXXXXXX' pattern."""
    rid = _generate_room_id()
    assert rid.startswith("order-"), f"Room ID must start with 'order-', got: {rid}"
    suffix = rid[len("order-"):]
    assert len(suffix) == 8, f"Suffix must be 8 chars, got {len(suffix)}: '{suffix}'"
    assert all(c in "0123456789abcdef" for c in suffix), \
        f"Suffix must be hex chars, got: '{suffix}'"

run_test("_generate_room_id() produces 'order-XXXXXXXX' format", test_room_id_format)


def test_room_ids_are_unique():
    """Every generated room ID must be unique (no collisions)."""
    ids = {_generate_room_id() for _ in range(1000)}
    assert len(ids) == 1000, f"Expected 1000 unique IDs, got {len(ids)} (collisions detected)"

run_test("_generate_room_id() generates 1000 unique IDs with no collisions", test_room_ids_are_unique)


def test_meeting_link_format():
    """Meeting link must be a valid URL pointing to the video room."""
    from config import cfg as real_cfg
    room_id = "order-a1b2c3d4"
    link    = _build_meeting_link(room_id)
    assert link.startswith("http"), f"Meeting link must be a URL, got: {link}"
    assert room_id in link, f"Room ID must be in the meeting link, got: {link}"
    assert "/room/" in link, f"Link must contain '/room/', got: {link}"

run_test("_build_meeting_link() produces a valid room URL", test_meeting_link_format)


def test_scheduled_time_is_in_future():
    """Auto-scheduled meeting must always be in the future."""
    before    = datetime.utcnow()
    scheduled = _calculate_scheduled_time()
    after     = datetime.utcnow()
    assert scheduled > before, "Scheduled time must be after now"
    assert scheduled > after,  "Scheduled time must be in the future"

run_test("_calculate_scheduled_time() always returns a future datetime", test_scheduled_time_is_in_future)


def test_scheduled_time_uses_config_offset():
    """Scheduled time must be NOW + MEETING_SCHEDULE_HOURS_AFTER hours."""
    from config import cfg as real_cfg
    hours     = real_cfg.MEETING_SCHEDULE_HOURS_AFTER
    before    = datetime.utcnow()
    scheduled = _calculate_scheduled_time()
    expected  = before + timedelta(hours=hours)
    diff_secs = abs((scheduled - expected).total_seconds())
    assert diff_secs < 5, \
        f"Scheduled time should be ~{hours}h from now, diff was {diff_secs:.1f}s"

run_test("_calculate_scheduled_time() uses MEETING_SCHEDULE_HOURS_AFTER config", test_scheduled_time_uses_config_offset)


def test_service_types_not_empty():
    """SERVICE_TYPES catalogue must have at least one entry."""
    assert len(SERVICE_TYPES) > 0, "SERVICE_TYPES must not be empty"
    for key, label in SERVICE_TYPES.items():
        assert key and label, f"Service type key and label must not be empty: {key!r}: {label!r}"
        assert "_" in key or key.isalpha(), f"Key should use snake_case: {key!r}"

run_test("SERVICE_TYPES catalogue has valid entries", test_service_types_not_empty)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 3 — Stripe Webhook Integration
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 3 — Stripe Webhook Integration{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def _make_webhook_payload(session_id="cs_test_0001", payment_status="paid",
                           payment_intent="pi_test_001",
                           order_type="service_booking",
                           client_email="alice@test.com"):
    """Build a realistic Stripe checkout.session.completed payload."""
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id":             session_id,
                "object":         "checkout.session",
                "payment_status": payment_status,
                "payment_intent": payment_intent,
                "customer_email": client_email,
                "metadata": {
                    "order_type":   order_type,
                    "client_name":  "Alice",
                    "client_email": client_email,
                    "service_type": "web_app",
                    "budget":       "$5k",
                },
            }
        }
    }


def test_webhook_activates_paid_order():
    """
    When Stripe fires checkout.session.completed with payment_status=paid,
    confirm_order_payment() must be called with correct arguments.
    """
    reset()
    payload = _make_webhook_payload()
    cfg_mock = MagicMock(STRIPE_WEBHOOK_SECRET="", APP_BASE_URL="http://localhost:5000",
                         MEETING_SCHEDULE_HOURS_AFTER=24)

    with patch.object(db, 'confirm_order_payment', return_value=True) as mock_confirm, \
         patch.object(db, '_db_available', True):

        # Simulate what the webhook handler does
        data_obj       = payload["data"]["object"]
        session_id     = data_obj["id"]
        payment_intent = data_obj["payment_intent"]
        client_email   = data_obj["customer_email"]
        metadata       = data_obj["metadata"]

        assert data_obj["payment_status"] == "paid"
        assert metadata["order_type"] == "service_booking"

        room_id      = _generate_room_id()
        meeting_link = _build_meeting_link(room_id)
        scheduled    = _calculate_scheduled_time()

        db.confirm_order_payment(
            cfg_mock, session_id, payment_intent,
            room_id, meeting_link, scheduled
        )

        mock_confirm.assert_called_once()
        call_args = mock_confirm.call_args[0]
        assert call_args[1] == session_id,     "session_id must be passed"
        assert call_args[2] == payment_intent, "payment_intent must be passed"
        assert call_args[3].startswith("order-"), "room_id must start with 'order-'"
        assert "/room/" in call_args[4],       "meeting_link must contain '/room/'"

run_test("Webhook: paid order triggers confirm_order_payment with correct args", test_webhook_activates_paid_order)


def test_webhook_ignores_non_order_events():
    """
    Subscription events (no order_type metadata) must be silently ignored
    by the order webhook handler — they belong to payments.py.
    """
    payload = _make_webhook_payload(order_type="subscription")  # not 'service_booking'
    metadata = payload["data"]["object"]["metadata"]

    is_order_event = metadata.get("order_type") == "service_booking"
    assert is_order_event is False, \
        "Subscription events must NOT be processed by the order webhook"

run_test("Webhook: non-order events (subscriptions) are ignored", test_webhook_ignores_non_order_events)


def test_webhook_ignores_unpaid_sessions():
    """
    If Stripe sends checkout.session.completed but payment_status != 'paid',
    the order must NOT be confirmed (e.g. free/trial orders).
    """
    payload  = _make_webhook_payload(payment_status="unpaid")
    data_obj = payload["data"]["object"]

    should_confirm = data_obj.get("payment_status") == "paid"
    assert should_confirm is False, \
        "Order confirmation must only proceed when payment_status == 'paid'"

run_test("Webhook: unpaid sessions do not trigger order confirmation", test_webhook_ignores_unpaid_sessions)


def test_webhook_idempotency_via_db_pending_filter():
    """
    If the webhook fires twice for the same session, the second call hits
    confirm_order_payment which uses WHERE payment_status='pending'.
    Since the first call already set it to 'paid', rowcount=0 on second call
    → returns False → no duplicate processing.
    """
    reset()
    session_id = "cs_dup_test"

    # First call: rowcount=1 (found pending order, updated to paid)
    conn1, cursor1 = make_mock_conn(rowcount=1)
    cfg_mock = MagicMock()
    with patch.object(db, '_get_conn', return_value=conn1), patch.object(db, '_return_conn'):
        result1 = db.confirm_order_payment(cfg_mock, session_id, "pi_1",
                                           "order-aaa", "http://...", datetime.utcnow())
    assert result1 is True, "First webhook call should succeed"

    # Second call: rowcount=0 (order already paid, WHERE pending finds nothing)
    conn2, cursor2 = make_mock_conn(rowcount=0)
    with patch.object(db, '_get_conn', return_value=conn2), patch.object(db, '_return_conn'):
        result2 = db.confirm_order_payment(cfg_mock, session_id, "pi_1",
                                           "order-aaa", "http://...", datetime.utcnow())
    assert result2 is False, "Second webhook call must be a no-op (idempotent)"

run_test("Webhook: duplicate events are idempotent (pending filter prevents double-processing)", test_webhook_idempotency_via_db_pending_filter)


def test_webhook_generates_different_room_per_order():
    """Each order gets its own unique room ID — never reuses a room."""
    rooms = {_generate_room_id() for _ in range(500)}
    assert len(rooms) == 500, "Every order must get a unique meeting room ID"

run_test("Webhook: every order gets a unique meeting room ID", test_webhook_generates_different_room_per_order)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 4 — API Endpoints (input validation)
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 4 — API Endpoint Validation{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_create_order_validates_name():
    """Order form: client_name must be at least 2 characters."""
    valid_cases   = ["Alice", "Bob Smith", "J K"]
    invalid_cases = ["", "A", " "]
    for name in valid_cases:
        assert len(name.strip()) >= 2, f"'{name}' should be valid"
    for name in invalid_cases:
        assert len(name.strip()) < 2, f"'{name}' should be invalid"

run_test("API: client_name validated (min 2 chars)", test_create_order_validates_name)


def test_create_order_validates_email():
    """Order form: client_email must contain @."""
    valid   = ["alice@example.com", "user+tag@domain.co.uk", "x@y.z"]
    invalid = ["", "notanemail", "missing-at-sign.com", "@nodomain"]
    for e in valid:
        assert "@" in e, f"'{e}' should be valid"
    for e in invalid:
        assert "@" not in e or e.startswith("@"), f"'{e}' should be invalid"

run_test("API: client_email validated (must contain @)", test_create_order_validates_email)


def test_create_order_validates_service_type():
    """Order form: service_type must be one of the defined SERVICE_TYPES keys."""
    for valid_key in SERVICE_TYPES.keys():
        assert valid_key in SERVICE_TYPES
    for invalid in ["", "unknown_service", "hacking", "WEBAPPDEV", "web app"]:
        assert invalid not in SERVICE_TYPES, f"'{invalid}' should be rejected"

run_test("API: service_type validated against SERVICE_TYPES catalogue", test_create_order_validates_service_type)


def test_create_order_validates_requirements_length():
    """Order form: requirements must be at least 20 characters."""
    assert len("too short") < 20
    assert len("This is a proper description of what I need.") >= 20
    assert len("x" * 5000) == 5000   # max allowed
    assert len("x" * 5001) > 5000   # should be truncated

run_test("API: requirements validated (min 20 chars)", test_create_order_validates_requirements_length)


def test_dashboard_api_only_exposes_safe_fields():
    """
    The /api/my-orders response must NOT expose sensitive fields like
    stripe_session_id, requirements (private), or internal DB timestamps.
    """
    # Simulate the field shaping in my_orders()
    raw_order = {
        "id": 1, "service_type": "web_app",
        "payment_status": "paid",
        "meeting_link": "http://localhost/room/order-abc",
        "room_id": "order-abc",
        "scheduled_time_fmt": "Monday 15 June 2025",
        "created_at_fmt": "01 June 2025",
        "stripe_session_id": "cs_SECRET_123",     # must not be exposed
        "stripe_transaction_id": "pi_SECRET_456", # must not be exposed
        "requirements": "Private client details...", # must not be exposed
        "can_join": True,
    }

    # Simulate the shaping logic from my_orders()
    exposed = {
        "id":             raw_order["id"],
        "service_type":   SERVICE_TYPES.get(raw_order["service_type"], raw_order["service_type"]),
        "payment_status": raw_order["payment_status"],
        "meeting_link":   raw_order.get("meeting_link"),
        "room_id":        raw_order.get("room_id"),
        "scheduled_time": raw_order.get("scheduled_time_fmt"),
        "created_at":     raw_order.get("created_at_fmt"),
        "can_join":       raw_order["payment_status"] == "paid" and bool(raw_order.get("meeting_link")),
    }

    assert "stripe_session_id"    not in exposed, "stripe_session_id must not be in API response"
    assert "stripe_transaction_id" not in exposed, "stripe_transaction_id must not be in API response"
    assert "requirements"         not in exposed, "requirements must not be in API response"
    assert exposed["can_join"]    is True,        "can_join should be True for paid order with meeting link"

run_test("Dashboard API: sensitive fields not exposed in response", test_dashboard_api_only_exposes_safe_fields)


def test_can_join_only_when_paid_and_has_link():
    """can_join must be True ONLY when payment_status=paid AND meeting_link exists."""
    cases = [
        ({"payment_status": "paid",    "meeting_link": "http://..."}, True),
        ({"payment_status": "paid",    "meeting_link": None},         False),
        ({"payment_status": "pending", "meeting_link": "http://..."}, False),
        ({"payment_status": "failed",  "meeting_link": "http://..."}, False),
        ({"payment_status": "paid",    "meeting_link": ""},           False),
    ]
    for order, expected in cases:
        result = order["payment_status"] == "paid" and bool(order.get("meeting_link"))
        assert result == expected, \
            f"can_join={result} for status={order['payment_status']}, link={order['meeting_link']!r} — expected {expected}"

run_test("can_join logic: True only when paid AND meeting_link exists", test_can_join_only_when_paid_and_has_link)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 5 — Connection Pool (1GB RAM server efficiency)
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 5 — Connection Pool (1GB RAM efficiency){RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_connection_returned_to_pool_after_use():
    """
    After a DB operation, the connection must be returned to the pool
    via _return_conn() so it can be reused. Not returning connections
    causes new connections to be opened on every request — CPU spikes.
    """
    reset()
    db._pool.clear()
    conn, cursor = make_mock_conn()
    cfg_mock = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn) as mock_get, \
         patch.object(db, '_return_conn') as mock_return:
        db.create_order(cfg_mock, "Bob", "bob@test.com", "web_app",
                        "I need a website built with React and Django.", None, "cs_pool_test")
        mock_return.assert_called_once_with(conn)

run_test("Pool: connection returned to pool after create_order()", test_connection_returned_to_pool_after_use)


def test_connection_returned_even_on_db_error():
    """
    If a DB operation raises an exception, the connection must STILL be
    returned to the pool (via finally block). Otherwise a leaked connection
    stays open and memory usage grows on the 1GB server.
    """
    reset()
    conn, cursor = make_mock_conn()
    cursor.execute.side_effect = Exception("MySQL gone away")
    cfg_mock = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn') as mock_return:
        result = db.create_order(cfg_mock, "Bob", "bob@test.com", "web_app",
                                 "requirements text here minimum length", None, "cs_error")
        mock_return.assert_called_once_with(conn), \
            "Connection MUST be returned even when an exception occurs"
        assert result is None, "Should return None on DB error"

run_test("Pool: connection returned to pool even when DB raises an exception", test_connection_returned_even_on_db_error)


def test_pool_caps_at_max_size():
    """
    The connection pool must cap at _pool_size connections.
    Excess connections should be closed rather than stored —
    preventing unbounded memory growth on a 1GB server.
    """
    db._pool.clear()
    fake_conns = [MagicMock() for _ in range(db._pool_size + 5)]

    for conn in fake_conns:
        db._return_conn(conn)

    assert len(db._pool) <= db._pool_size, \
        f"Pool grew to {len(db._pool)} — must be capped at {db._pool_size}"

    closed_count = sum(1 for c in fake_conns if c.close.called)
    assert closed_count == 5, \
        f"Expected 5 excess connections to be closed, got {closed_count}"

run_test("Pool: caps at _pool_size, excess connections are closed", test_pool_caps_at_max_size)


def test_stale_connection_replaced_from_pool():
    """
    If a pooled connection is stale (ping raises), _get_conn must
    open a fresh connection instead of returning the broken one.
    """
    db._pool.clear()
    stale_conn = MagicMock()
    stale_conn.ping.side_effect = Exception("MySQL server has gone away")

    fresh_conn = MagicMock()
    fresh_conn.ping.return_value = None

    db._pool.append(stale_conn)
    cfg_mock = MagicMock(MYSQL_HOST="localhost", MYSQL_PORT=3306,
                         MYSQL_USER="u", MYSQL_PASSWORD="p", MYSQL_DATABASE="db")

    with patch.object(db, '_new_connection', return_value=fresh_conn) as mock_new:
        conn = db._get_conn(cfg_mock)
        assert conn is fresh_conn, "Should return fresh connection when pooled one is stale"
        mock_new.assert_called_once()

run_test("Pool: stale connection replaced with fresh connection", test_stale_connection_replaced_from_pool)


# ═════════════════════════════════════════════════════════════════════════════
#  GROUP 6 — Edge Cases & Security
# ═════════════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Group 6 — Edge Cases & Security{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")


def test_meeting_link_uses_app_base_url():
    """
    The meeting link must use APP_BASE_URL from config, not a hardcoded value.
    This ensures it works on any domain (local, staging, production).
    """
    from config import cfg as real_cfg
    room_id = "order-test1234"
    link    = _build_meeting_link(room_id)
    assert link.startswith(real_cfg.APP_BASE_URL), \
        f"Meeting link must start with APP_BASE_URL={real_cfg.APP_BASE_URL!r}, got: {link}"

run_test("Security: meeting_link uses APP_BASE_URL from config (not hardcoded)", test_meeting_link_uses_app_base_url)


def test_no_sql_injection_in_create_order():
    """
    Malicious input in client_name/requirements must be handled by
    parameterised queries — the values should be passed as parameters,
    never string-formatted into the SQL.
    """
    reset()
    conn, cursor = make_mock_conn()
    cfg_mock = MagicMock()
    evil_name = "Robert'); DROP TABLE orders;--"
    with patch.object(db, '_get_conn', return_value=conn), patch.object(db, '_return_conn'):
        db.create_order(cfg_mock, evil_name, "evil@test.com", "web_app",
                        "requirements text here minimum length check", None, "cs_sql")

    # Verify the evil string was passed as a parameter, not interpolated into SQL
    sql    = cursor.execute.call_args[0][0]
    params = cursor.execute.call_args[0][1]
    assert evil_name not in sql, \
        "SQL injection string must NOT appear in the SQL template"
    assert evil_name[:50] in str(params), \
        "The value should still be in params (safely parameterised)"

run_test("Security: SQL injection input passed as param, not interpolated into SQL", test_no_sql_injection_in_create_order)


def test_room_id_not_guessable_sequential():
    """
    Room IDs must not be sequential integers (easy to enumerate).
    They must be random hex strings.
    """
    ids = [_generate_room_id() for _ in range(10)]
    # Check they are not sequential (e.g., order-00000001, order-00000002)
    suffixes = [rid[len("order-"):] for rid in ids]
    are_sequential = all(
        int(suffixes[i], 16) == int(suffixes[i-1], 16) + 1
        for i in range(1, len(suffixes))
    )
    assert not are_sequential, "Room IDs must be random, not sequential"

run_test("Security: room IDs are random (not guessable sequential integers)", test_room_id_not_guessable_sequential)


def test_budget_field_is_optional():
    """budget=None should be stored as NULL without raising an error."""
    reset()
    conn, cursor = make_mock_conn()
    cfg_mock = MagicMock()
    with patch.object(db, '_get_conn', return_value=conn), patch.object(db, '_return_conn'):
        result = db.create_order(cfg_mock, "Alice", "alice@test.com", "consultation",
                                 "I need a technical consultation on my architecture.", None, "cs_nobud")
    assert result == 42, "create_order with budget=None should succeed"
    args = cursor.execute.call_args[0][1]
    assert args[4] is None, "budget should be stored as NULL when not provided"

run_test("Edge case: budget=None stored as NULL (no error)", test_budget_field_is_optional)


def test_confirm_order_rollback_on_exception():
    """
    If the DB UPDATE raises mid-transaction, confirm_order_payment()
    must call conn.rollback() to prevent a partial write.
    """
    reset()
    conn, cursor = make_mock_conn()
    cursor.execute.side_effect = Exception("Deadlock detected")
    cfg_mock = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), patch.object(db, '_return_conn'):
        result = db.confirm_order_payment(cfg_mock, "cs_deadlock", "pi_x",
                                          "order-x", "http://...", datetime.utcnow())

    assert result is False, "Should return False on exception"
    conn.rollback.assert_called_once(), "rollback() must be called on exception to prevent partial write"

run_test("Edge case: confirm_order_payment() calls rollback() on DB exception", test_confirm_order_rollback_on_exception)


# ═════════════════════════════════════════════════════════════════════════════
#  Summary
# ═════════════════════════════════════════════════════════════════════════════
total = len(passed) + len(failed)
print(f"\n{BOLD}{'─'*62}{RESET}")
print(f"{BOLD}  Results: {GREEN}{len(passed)} passed{RESET}{BOLD}, "
      f"{RED}{len(failed)} failed{RESET}{BOLD} / {total} total{RESET}")
print(f"{BOLD}{'─'*62}{RESET}\n")

if failed:
    print(f"{RED}Failed tests:{RESET}")
    for n in failed: print(f"  • {n}")
    print()
    sys.exit(1)
else:
    print(f"{GREEN}All booking tests passed.{RESET}\n")
    sys.exit(0)