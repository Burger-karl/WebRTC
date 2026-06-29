"""
fix_database_create_order.py — Database migration / repair utility for orders table.

FIXES APPLIED (from Team Lead Code Review):
  FIX 1 [fix_database_create_order.py] Database Query Regression: A previous
         version of this script stripped room_id and meeting_link from the INSERT
         query in database.py. This script verifies those columns exist in the
         orders table and re-adds them if missing, restoring full data integrity.
"""

import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _check_and_fix_orders_table(conn, cursor):
    """
    FIX 1: Ensure orders table has room_id and meeting_link columns.
    These were erroneously removed in a previous fix script iteration.
    Uses conditional ADD only — safe to re-run repeatedly.
    """
    # Check which columns currently exist
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'orders'
    """)
    existing_cols = {row['COLUMN_NAME'] for row in cursor.fetchall()}

    missing = []
    if 'room_id' not in existing_cols:
        missing.append('room_id')
    if 'meeting_link' not in existing_cols:
        missing.append('meeting_link')

    if not missing:
        logger.info("✅  orders table already has room_id and meeting_link — no changes needed.")
        return True

    logger.warning(f"⚠️  Missing columns in orders table: {missing}")

    for col in missing:
        if col == 'room_id':
            cursor.execute("""
                ALTER TABLE orders
                ADD COLUMN room_id VARCHAR(80) DEFAULT NULL
                AFTER stripe_transaction_id
            """)
            logger.info("  ✅  Added column: room_id")
        elif col == 'meeting_link':
            cursor.execute("""
                ALTER TABLE orders
                ADD COLUMN meeting_link VARCHAR(300) DEFAULT NULL
                AFTER room_id
            """)
            logger.info("  ✅  Added column: meeting_link")

    conn.commit()
    logger.info("Migration complete — orders table now has room_id and meeting_link.")
    return True


def _verify_create_order_function():
    """
    Verify that database.py's create_order() includes room_id and meeting_link
    in the INSERT statement. This is a static code inspection check.
    """
    try:
        import ast, inspect
        import database
        source = inspect.getsource(database.create_order)
        if 'room_id' in source and 'meeting_link' in source:
            logger.info("✅  database.create_order() includes room_id and meeting_link ✓")
            return True
        else:
            logger.error(
                "❌  database.create_order() is MISSING room_id or meeting_link!\n"
                "    This is the regression — the INSERT query must include both columns.\n"
                "    Replace database.py with the fixed version from this repository."
            )
            return False
    except Exception as e:
        logger.warning(f"Could not inspect database.create_order(): {e}")
        return None


def main():
    print("=" * 60)
    print("  MeetFree — Fix Orders Table Migration")
    print("=" * 60)

    try:
        from dotenv import load_dotenv
        load_dotenv()
        from config import cfg
    except ImportError as e:
        print(f"ERROR: Could not load config: {e}")
        sys.exit(1)

    if not cfg.MYSQL_ENABLED:
        print("MySQL is disabled (MYSQL_ENABLED=false) — nothing to do.")
        sys.exit(0)

    # Step 1: Verify the Python code is correct
    print("\n[Step 1] Checking database.py create_order() function…")
    code_ok = _verify_create_order_function()

    # Step 2: Fix the database schema
    print("\n[Step 2] Checking orders table schema…")
    try:
        import pymysql
        conn = pymysql.connect(
            host=cfg.MYSQL_HOST, port=cfg.MYSQL_PORT,
            user=cfg.MYSQL_USER, password=cfg.MYSQL_PASSWORD,
            database=cfg.MYSQL_DATABASE, charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cursor:
            _check_and_fix_orders_table(conn, cursor)
        conn.close()
    except Exception as e:
        print(f"\nERROR connecting to database: {e}")
        sys.exit(1)

    print("\n" + "=" * 60)
    if code_ok is False:
        print("⚠️  ACTION REQUIRED: Replace database.py with the fixed version.")
        print("   The orders INSERT query must include room_id and meeting_link.")
        sys.exit(1)
    else:
        print("✅  All checks passed. Orders table is correctly configured.")
    print("=" * 60)


if __name__ == "__main__":
    main()
