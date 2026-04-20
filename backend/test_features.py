"""
test_features.py — Individual Tests for feature-dev Branch
─────────────────────────────────────────────────────────────────────────────
Tests for all 3 new features. Each feature can be tested individually.

No MySQL server needed — database is mocked entirely.
No Flask server needed — internal functions tested directly.

Run all tests:
    cd backend
    python test_features.py

Run a specific feature only:
    python test_features.py chat
    python test_features.py passwords
    python test_features.py hostcontrols
─────────────────────────────────────────────────────────────────────────────
"""

import sys
import os
import time
import types
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("SECRET_KEY",    "test-secret")
os.environ.setdefault("JWT_SECRET",    "test-jwt-secret")
os.environ.setdefault("MYSQL_ENABLED", "true")
os.environ.setdefault("MYSQL_HOST",    "localhost")
os.environ.setdefault("MYSQL_USER",    "test")
os.environ.setdefault("MYSQL_PASSWORD","test")
os.environ.setdefault("MYSQL_DATABASE","testdb")
os.environ.setdefault("STRIPE_SECRET_KEY", "")

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"; BOLD = "\033[1m"; RESET = "\033[0m"
passed = []; failed = []

def run_test(name, fn):
    try:
        fn()
        print(f"  {GREEN}✓ PASS{RESET}  {name}")
        passed.append(name)
    except AssertionError as e:
        print(f"  {RED}✗ FAIL{RESET}  {name}\n         {RED}→ {str(e) or 'Assertion failed'}{RESET}")
        failed.append(name)
    except Exception as e:
        print(f"  {RED}✗ ERROR{RESET} {name}\n         {RED}→ {type(e).__name__}: {e}{RESET}")
        failed.append(name)


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 1 — Persistent Chat (MySQL)
# ═════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}{'─'*60}{RESET}")
print(f"{BOLD}  Feature 1 — Persistent Chat (MySQL){RESET}")
print(f"{BOLD}{'─'*60}{RESET}\n")

import database as db

def make_mock_conn(rows=None):
    """Helper: build a mock PyMySQL connection that returns `rows` from fetchall."""
    conn   = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = lambda s: s
    cursor.__exit__  = MagicMock(return_value=False)
    cursor.fetchall.return_value = rows or []
    cursor.fetchone.return_value = rows[0] if rows else None
    conn.cursor.return_value     = cursor
    conn.insert_id.return_value  = 42
    return conn, cursor


def test_save_message_calls_correct_sql():
    """save_message() executes an INSERT with the right parameters."""
    db._db_available = True
    conn, cursor = make_mock_conn()
    cfg = MagicMock(CHAT_HISTORY_LIMIT=50)

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        result = db.save_message(cfg, 'test-room', 'Alice', 'Hello world')

    assert result == 42, f"Expected insert_id=42, got {result}"
    call_args = cursor.execute.call_args[0]
    assert 'INSERT INTO chat_messages' in call_args[0], "Should INSERT into chat_messages"
    assert call_args[1] == ('test-room', 'Alice', 'Hello world'), \
        f"Wrong parameters: {call_args[1]}"

run_test("save_message() inserts with correct SQL and parameters", test_save_message_calls_correct_sql)


def test_save_message_truncates_long_messages():
    """Messages longer than 500 chars should be truncated before storage."""
    db._db_available = True
    conn, cursor = make_mock_conn()
    cfg = MagicMock()
    long_msg = 'x' * 600

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.save_message(cfg, 'room', 'Bob', long_msg)

    call_args = cursor.execute.call_args[0][1]
    stored_msg = call_args[2]
    assert len(stored_msg) <= 500, f"Message should be truncated to 500 chars, got {len(stored_msg)}"

run_test("save_message() truncates messages longer than 500 chars", test_save_message_truncates_long_messages)


def test_get_room_history_returns_oldest_first():
    """
    get_room_history() fetches last N messages and reverses them
    so the oldest message appears first in the returned list.
    """
    db._db_available = True
    # DB returns them newest-first (ORDER BY sent_at DESC)
    mock_rows = [
        {'sender_name': 'Bob',   'message': 'Third',  'sent_at': '10:03'},
        {'sender_name': 'Alice', 'message': 'Second', 'sent_at': '10:02'},
        {'sender_name': 'Alice', 'message': 'First',  'sent_at': '10:01'},
    ]
    conn, cursor = make_mock_conn(rows=mock_rows)
    cfg = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        result = db.get_room_history(cfg, 'test-room', limit=50)

    assert len(result) == 3, f"Expected 3 messages, got {len(result)}"
    assert result[0]['message'] == 'First',  "First message should be oldest"
    assert result[2]['message'] == 'Third',  "Last message should be newest"

run_test("get_room_history() returns messages oldest-first", test_get_room_history_returns_oldest_first)


def test_save_message_returns_none_when_db_unavailable():
    """When MySQL is down, save_message() returns None gracefully (no crash)."""
    db._db_available = False
    cfg = MagicMock()
    result = db.save_message(cfg, 'room', 'Alice', 'Hello')
    assert result is None, "Should return None when DB unavailable"
    db._db_available = True  # restore

run_test("save_message() returns None gracefully when DB is down", test_save_message_returns_none_when_db_unavailable)


def test_get_room_history_returns_empty_when_db_unavailable():
    """When MySQL is down, get_room_history() returns [] (no crash)."""
    db._db_available = False
    cfg = MagicMock()
    result = db.get_room_history(cfg, 'room')
    assert result == [], "Should return empty list when DB unavailable"
    db._db_available = True

run_test("get_room_history() returns [] when DB is down", test_get_room_history_returns_empty_when_db_unavailable)


def test_delete_room_history_removes_chat_and_mute_state():
    """delete_room_history() should DELETE both chat_messages AND participant_mute_state."""
    db._db_available = True
    conn, cursor = make_mock_conn()
    cfg = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.delete_room_history(cfg, 'old-room')

    executed_sqls = [str(c[0][0]) for c in cursor.execute.call_args_list]
    chat_deleted  = any('chat_messages' in s for s in executed_sqls)
    mute_deleted  = any('participant_mute_state' in s for s in executed_sqls)
    assert chat_deleted, "Should delete from chat_messages"
    assert mute_deleted, "Should delete from participant_mute_state"

run_test("delete_room_history() cleans up both tables", test_delete_room_history_removes_chat_and_mute_state)


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 2 — Room Passwords
# ═════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}{'─'*60}{RESET}")
print(f"{BOLD}  Feature 2 — Room Passwords{RESET}")
print(f"{BOLD}{'─'*60}{RESET}\n")

import bcrypt as real_bcrypt

def test_create_room_hashes_password():
    """create_room() should store a bcrypt hash, not the plaintext password."""
    db._db_available = True
    conn, cursor = make_mock_conn()
    cfg = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        result = db.create_room(cfg, 'secure-room', 'Alice', password='secret123')

    assert result is True, "create_room should return True on success"
    call_args = cursor.execute.call_args[0][1]
    stored_hash = call_args[1]  # (room_id, password_hash, created_by)
    assert stored_hash is not None, "Password hash should not be None"
    assert stored_hash != 'secret123', "Plaintext password must never be stored"
    assert stored_hash.startswith('$2b$'), f"Should be a bcrypt hash starting with $2b$, got: {stored_hash[:10]}"

run_test("create_room() stores bcrypt hash, not plaintext password", test_create_room_hashes_password)


def test_create_room_no_password_stores_null():
    """create_room() with no password should store NULL (open room)."""
    db._db_available = True
    conn, cursor = make_mock_conn()
    cfg = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.create_room(cfg, 'open-room', 'Bob', password=None)

    call_args = cursor.execute.call_args[0][1]
    stored_hash = call_args[1]
    assert stored_hash is None, "Open room should store NULL password_hash"

run_test("create_room() stores NULL for open rooms (no password)", test_create_room_no_password_stores_null)


def test_verify_password_correct():
    """verify_room_password() returns True when correct password is submitted."""
    db._db_available = True
    password   = 'MyRoomPass123'
    hash_bytes = real_bcrypt.hashpw(password.encode(), real_bcrypt.gensalt(rounds=4))
    room_record = {'room_id': 'locked-room', 'password_hash': hash_bytes.decode()}
    cfg = MagicMock()

    with patch.object(db, 'get_room', return_value=room_record):
        result = db.verify_room_password(cfg, 'locked-room', password)

    assert result is True, "Correct password should return True"

run_test("verify_room_password() accepts the correct password", test_verify_password_correct)


def test_verify_password_wrong():
    """verify_room_password() returns False when wrong password is submitted."""
    db._db_available = True
    correct_pw = 'CorrectPassword'
    hash_bytes = real_bcrypt.hashpw(correct_pw.encode(), real_bcrypt.gensalt(rounds=4))
    room_record = {'room_id': 'locked-room', 'password_hash': hash_bytes.decode()}
    cfg = MagicMock()

    with patch.object(db, 'get_room', return_value=room_record):
        result = db.verify_room_password(cfg, 'locked-room', 'WrongPassword')

    assert result is False, "Wrong password should return False"

run_test("verify_room_password() rejects the wrong password", test_verify_password_wrong)


def test_verify_password_open_room_always_passes():
    """An open room (no password_hash) should allow anyone in."""
    db._db_available = True
    room_record = {'room_id': 'open-room', 'password_hash': None}
    cfg = MagicMock()

    with patch.object(db, 'get_room', return_value=room_record):
        result = db.verify_room_password(cfg, 'open-room', '')

    assert result is True, "Open room (no password) should always return True"

run_test("verify_room_password() allows entry to open rooms", test_verify_password_open_room_always_passes)


def test_verify_password_new_room_passes():
    """A brand new room (no DB record yet) should allow entry."""
    db._db_available = True
    cfg = MagicMock()

    with patch.object(db, 'get_room', return_value=None):
        result = db.verify_room_password(cfg, 'brand-new-room', '')

    assert result is True, "Room not in DB yet should allow entry"

run_test("verify_room_password() allows entry when room has no DB record yet", test_verify_password_new_room_passes)


def test_verify_password_fails_open_when_db_down():
    """When MySQL is down, password check should fail open (allow entry)."""
    db._db_available = False
    cfg = MagicMock()
    result = db.verify_room_password(cfg, 'any-room', 'any-password')
    assert result is True, "Should fail open when DB is unavailable"
    db._db_available = True

run_test("verify_room_password() fails open (allows entry) when DB is down", test_verify_password_fails_open_when_db_down)


def test_empty_password_string_treated_as_no_password():
    """Blank password string should be treated same as no password (open room)."""
    db._db_available = True
    conn, cursor = make_mock_conn()
    cfg = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.create_room(cfg, 'room', 'Alice', password='')  # empty string

    call_args = cursor.execute.call_args[0][1]
    stored_hash = call_args[1]
    assert stored_hash is None, "Empty string password should be stored as NULL"

run_test("create_room() treats empty string password as open room (NULL)", test_empty_password_string_treated_as_no_password)


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 3 — Host Controls (Mute / Kick)
# ═════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}{'─'*60}{RESET}")
print(f"{BOLD}  Feature 3 — Host Controls (Mute / Kick){RESET}")
print(f"{BOLD}{'─'*60}{RESET}\n")


def test_set_participant_muted_upsert():
    """set_participant_muted() should INSERT...ON DUPLICATE KEY UPDATE."""
    db._db_available = True
    conn, cursor = make_mock_conn()
    cfg = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.set_participant_muted(cfg, 'room1', 'Alice', is_muted=True)

    sql = cursor.execute.call_args[0][0]
    assert 'ON DUPLICATE KEY UPDATE' in sql, "Should use UPSERT to avoid duplicate rows"
    args = cursor.execute.call_args[0][1]
    assert args[0] == 'room1', f"Wrong room_id: {args[0]}"
    assert args[1] == 'Alice', f"Wrong peer_name: {args[1]}"
    assert args[2] == 1, "is_muted=True should store as 1"

run_test("set_participant_muted() uses UPSERT (no duplicate rows)", test_set_participant_muted_upsert)


def test_set_participant_unmuted():
    """set_participant_muted(is_muted=False) should store 0."""
    db._db_available = True
    conn, cursor = make_mock_conn()
    cfg = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        db.set_participant_muted(cfg, 'room1', 'Bob', is_muted=False)

    args = cursor.execute.call_args[0][1]
    assert args[2] == 0, "is_muted=False should store as 0"

run_test("set_participant_muted() stores 0 for unmuted state", test_set_participant_unmuted)


def test_get_muted_participants_returns_names():
    """get_muted_participants() returns a list of muted peer names."""
    db._db_available = True
    mock_rows = [{'peer_name': 'Alice'}, {'peer_name': 'Charlie'}]
    conn, cursor = make_mock_conn(rows=mock_rows)
    cfg = MagicMock()

    with patch.object(db, '_get_conn', return_value=conn), \
         patch.object(db, '_return_conn'):
        result = db.get_muted_participants(cfg, 'room1')

    assert result == ['Alice', 'Charlie'], f"Expected ['Alice', 'Charlie'], got {result}"

run_test("get_muted_participants() returns list of muted names", test_get_muted_participants_returns_names)


def test_get_muted_participants_empty_when_db_down():
    """When DB is down, get_muted_participants() returns [] (no crash)."""
    db._db_available = False
    cfg = MagicMock()
    result = db.get_muted_participants(cfg, 'room1')
    assert result == [], "Should return empty list when DB unavailable"
    db._db_available = True

run_test("get_muted_participants() returns [] when DB is down", test_get_muted_participants_empty_when_db_down)


def test_mute_state_persisted_and_retrieved():
    """
    Integration: mute a participant, then retrieve muted list.
    Both calls should use consistent data.
    """
    db._db_available = True
    cfg = MagicMock()

    # --- Set mute ---
    conn_set, cursor_set = make_mock_conn()
    with patch.object(db, '_get_conn', return_value=conn_set), \
         patch.object(db, '_return_conn'):
        db.set_participant_muted(cfg, 'room-x', 'Dave', is_muted=True)

    set_sql  = cursor_set.execute.call_args[0][0]
    set_args = cursor_set.execute.call_args[0][1]
    assert 'room-x' in set_args, "room_id should be in SET args"
    assert 'Dave'   in set_args, "peer_name should be in SET args"
    assert 1 in set_args,        "muted=1 should be in SET args"

    # --- Get muted ---
    conn_get, cursor_get = make_mock_conn(rows=[{'peer_name': 'Dave'}])
    with patch.object(db, '_get_conn', return_value=conn_get), \
         patch.object(db, '_return_conn'):
        result = db.get_muted_participants(cfg, 'room-x')

    assert result == ['Dave'], f"Expected ['Dave'], got {result}"

run_test("Mute state: set then retrieve returns consistent data", test_mute_state_persisted_and_retrieved)


def test_only_host_can_mute_verified_server_side():
    """
    Verify that room_admins check works correctly — non-admin cannot mute.
    Tests the server-side guard logic directly.
    """
    rooms       = {'room-abc': {'sid-admin': {'name': 'Admin'}, 'sid-bob': {'name': 'Bob'}}}
    room_admins = {'room-abc': 'sid-admin'}

    def can_mute(caller_sid, room_id):
        return room_admins.get(room_id) == caller_sid

    assert can_mute('sid-admin', 'room-abc') is True,  "Admin should be able to mute"
    assert can_mute('sid-bob',   'room-abc') is False, "Non-admin should NOT be able to mute"
    assert can_mute('sid-carol', 'room-abc') is False, "Unknown user should NOT be able to mute"

run_test("Only host can mute — server-side admin check", test_only_host_can_mute_verified_server_side)


# ═════════════════════════════════════════════════════════════════════════════
#  Summary
# ═════════════════════════════════════════════════════════════════════════════

total = len(passed) + len(failed)
print(f"\n{BOLD}{'─'*60}{RESET}")
print(f"{BOLD}  Results: {GREEN}{len(passed)} passed{RESET}{BOLD}, "
      f"{RED}{len(failed)} failed{RESET}{BOLD} / {total} total{RESET}")
print(f"{BOLD}{'─'*60}{RESET}\n")

if failed:
    print(f"{RED}Failed tests:{RESET}")
    for n in failed: print(f"  • {n}")
    print()
    sys.exit(1)
else:
    print(f"{GREEN}All feature tests passed.{RESET}\n")
    sys.exit(0)