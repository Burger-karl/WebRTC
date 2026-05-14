"""
Run this script from your backend/ folder:
    python fix_database_create_order.py

It finds and fixes the create_order() function in database.py to remove
the unexpected 'room_id' parameter that is causing the booking error.
"""
import re, os, sys, shutil
from datetime import datetime

db_path = os.path.join(os.path.dirname(__file__), 'database.py')
if not os.path.exists(db_path):
    print(f"ERROR: database.py not found at {db_path}")
    sys.exit(1)

with open(db_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Check what signature is currently in the file
match = re.search(r'def create_order\([^)]+\)', content)
if match:
    print(f"Current create_order signature:\n  {match.group()}\n")
else:
    print("ERROR: Could not find create_order in database.py")
    sys.exit(1)

# If room_id is in the signature, fix it
if 'room_id' in match.group():
    print("Found bad signature with room_id — fixing...")

    # Backup first
    backup = db_path + f'.backup_{datetime.now().strftime("%H%M%S")}'
    shutil.copy(db_path, backup)
    print(f"Backup saved: {backup}")

    # Replace the entire create_order function
    old_pattern = r'def create_order\(cfg[^)]*room_id[^)]*\).*?(?=\ndef |\Z)'
    new_func = '''def create_order(cfg, client_name: str, client_email: str, service_type: str,
                 requirements: str, budget: str, stripe_session_id: str):
    """Create a pending order record before Stripe payment."""
    import pymysql
    if not _db_available:
        return None
    conn = None
    try:
        conn = _get_conn(cfg)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO orders
                    (client_name, client_email, service_type, requirements,
                     budget, stripe_session_id, payment_status)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            """, (
                client_name[:100],
                client_email[:120].lower(),
                service_type[:100],
                requirements[:5000],
                budget[:50] if budget else None,
                stripe_session_id[:200],
            ))
        conn.commit()
        return conn.insert_id()
    except Exception as e:
        logger.error(f"[BOOKING] create_order error: {e}")
        if conn:
            try: conn.rollback()
            except: pass
        return None
    finally:
        if conn:
            _return_conn(conn)

'''
    content = re.sub(old_pattern, new_func, content, flags=re.DOTALL)
    with open(db_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print("✓ database.py fixed successfully!")
    print("\nRestart your server: python server.py")
else:
    print("✓ create_order signature is already correct — no fix needed.")
    print("  The issue may be a cached .pyc file. Try:")
    print("  1. Delete backend/__pycache__/ folder")
    print("  2. Restart server: python server.py")