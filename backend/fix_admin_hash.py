"""
fix_admin_hash.py — Utility to repair a Room Admin's bcrypt password hash.

FIXES APPLIED (from Team Lead Code Review):
  FIX 1 [fix_admin_hash.py] Safe Line Parsing: Replaced .startswith() with
         re.match() so irregular spacing or duplicate keys in .env files
         are handled correctly without accidentally appending duplicate entries.
"""

import os
import re
import sys
import getpass
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _read_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


def _update_env_key(lines: list[str], key: str, value: str) -> list[str]:
    """
    FIX 1: Use re.match() to locate the key. .startswith() fails when
    there is leading whitespace or the key appears as a substring of another key.
    """
    pattern = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
    replaced = False
    new_lines = []
    for line in lines:
        if pattern.match(line) and not replaced:
            new_lines.append(f"{key}={value}\n")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"{key}={value}\n")
    return new_lines


def fix_hash_in_env(email: str, new_hash: str):
    for env_file in [".env", ".env.local"]:
        lines = _read_lines(env_file)
        if not lines:
            continue
        new_lines = _update_env_key(lines, "SUPER_ADMIN_PASSWORD_HASH", new_hash)
        with open(env_file, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(new_lines)
        logger.info(f"Updated {env_file}")


def fix_room_admin_hash_in_db(email: str, new_hash: str):
    """Update a Room Admin's bcrypt hash directly in the database."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from config import cfg
        import database as db

        db.init_db(cfg)
        import pymysql
        conn = pymysql.connect(
            host=cfg.MYSQL_HOST, port=cfg.MYSQL_PORT,
            user=cfg.MYSQL_USER, password=cfg.MYSQL_PASSWORD,
            database=cfg.MYSQL_DATABASE, charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE room_admins SET password_hash = %s WHERE email = %s",
                (new_hash, email.lower().strip())
            )
            rows = cur.rowcount
        conn.commit()
        conn.close()

        if rows:
            logger.info(f"Room Admin hash updated in DB for {email}")
        else:
            logger.warning(f"No Room Admin found in DB with email {email}")
        return rows > 0

    except Exception as e:
        logger.error(f"DB error: {e}")
        return False


def main():
    print("=" * 55)
    print("  MeetFree — Fix Admin Password Hash")
    print("=" * 55)

    try:
        import bcrypt
    except ImportError:
        print("ERROR: bcrypt not installed. Run: pip install bcrypt")
        sys.exit(1)

    print("\nWhich admin account needs a hash fix?")
    print("  1. Super Admin (stored in .env file)")
    print("  2. Room Admin (stored in database)")
    choice = input("Choice [1/2]: ").strip()

    email = input("Admin email: ").strip().lower()
    if not email or "@" not in email:
        print("ERROR: Invalid email.")
        sys.exit(1)

    password = getpass.getpass("New password (input hidden): ")
    if len(password) < 8:
        print("ERROR: Password must be at least 8 characters.")
        sys.exit(1)

    confirm = getpass.getpass("Confirm new password: ")
    if password != confirm:
        print("ERROR: Passwords do not match.")
        sys.exit(1)

    print("\nGenerating bcrypt hash…", end="", flush=True)
    new_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    print(" done.")
    print(f"Hash prefix: {new_hash[:7]}… ✓")

    if choice == "1":
        fix_hash_in_env(email, new_hash)
        print("\nHash written to .env / .env.local")
        print("Restart the server: docker compose up -d --force-recreate")
    elif choice == "2":
        ok = fix_room_admin_hash_in_db(email, new_hash)
        if ok:
            print("\nHash updated in DB — no server restart needed.")
        else:
            print("\nFailed — check email and DB connection.")
    else:
        print("Invalid choice.")
        sys.exit(1)


if __name__ == "__main__":
    main()
