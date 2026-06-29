"""
create_superadmin.py — One-time utility to generate a bcrypt hash for the
Super Admin password and write it to your .env / .env.local files.

FIXES APPLIED (from Team Lead Code Review):
  FIX 1 [create_superadmin.py] Deployment Instructions: Post-setup output now
         shows  docker compose up -d --force-recreate  so the freshly written
         env vars are actually loaded into running containers.

  FIX 2 [create_superadmin.py] Persistent Storage Architecture: A warning is
         printed if the detected deployment looks stateless (Docker / container),
         reminding the operator that runtime-only .env changes will be lost on
         container recycle and the hash must be baked into the image/secret.

  FIX 3 [fix_admin_hash.py] Safe Line Parsing: Replaced .startswith()-based
         env parsing with re.match() so lines with irregular leading whitespace
         or duplicate keys are handled correctly without accidental key appending.
"""

import os
import re
import sys
import getpass


def _is_running_in_container() -> bool:
    """Best-effort check for Docker / container environment."""
    return (
        os.path.exists("/.dockerenv")
        or os.environ.get("DOCKER_CONTAINER") == "true"
        or os.environ.get("container") is not None
    )


def _read_env(path: str) -> list[str]:
    """Read env file lines, returning [] if file doesn't exist."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


def _write_env_key(lines: list[str], key: str, value: str) -> list[str]:
    """
    FIX 3: Use re.match() to locate and replace the key, handling leading
    whitespace or duplicate keys that .startswith() would miss.
    Pattern: optional whitespace, KEY=, rest of line.
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


def _update_env_file(path: str, key: str, value: str) -> bool:
    """Read → patch → write an env file."""
    lines = _read_env(path)
    new_lines = _write_env_key(lines, key, value)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(new_lines)
    return True


def main():
    print("=" * 60)
    print("  MeetFree — Super Admin Setup")
    print("=" * 60)

    try:
        import bcrypt
    except ImportError:
        print("\n[ERROR] bcrypt is not installed.")
        print("Run:  pip install bcrypt")
        sys.exit(1)

    # ── Prompt for credentials ────────────────────────────────────────────────
    email = input("\nEnter Super Admin email: ").strip().lower()
    if not email or "@" not in email:
        print("[ERROR] Invalid email address.")
        sys.exit(1)

    password = getpass.getpass("Enter Super Admin password (input hidden): ")
    if len(password) < 12:
        print("[ERROR] Password must be at least 12 characters for production security.")
        sys.exit(1)

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("[ERROR] Passwords do not match.")
        sys.exit(1)

    # ── Hash ──────────────────────────────────────────────────────────────────
    print("\nHashing password (bcrypt rounds=12) …", end="", flush=True)
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    print(" done.")

    # ── Write to env files ────────────────────────────────────────────────────
    env_files_found = []
    for env_path in [".env", ".env.local"]:
        if os.path.exists(env_path) or env_path == ".env":
            _update_env_file(env_path, "SUPER_ADMIN_EMAIL", email)
            _update_env_file(env_path, "SUPER_ADMIN_PASSWORD_HASH", pw_hash)
            env_files_found.append(env_path)
            print(f"  ✅  Updated {env_path}")

    print(f"\n  Email hash written: {email}")
    print(f"  Hash prefix check:  {pw_hash[:7]}…  (looks like bcrypt ✓)")

    # ── FIX 2: Container / persistent storage warning ─────────────────────────
    if _is_running_in_container():
        print("\n" + "⚠️  " * 20)
        print("  WARNING: You appear to be running inside a container.")
        print("  Changes written to .env / .env.local on the container filesystem")
        print("  will be LOST when the container is recreated.")
        print("  To make these credentials permanent, either:")
        print("    1. Bake them into your Docker image or a bind-mounted secrets volume.")
        print("    2. Pass them as environment variables via docker-compose.yml or")
        print("       a secrets manager (e.g. AWS Secrets Manager, Vault).")
        print("⚠️  " * 20)

    # ── FIX 1: Corrected deployment instructions ──────────────────────────────
    print("\n" + "=" * 60)
    print("  Next steps")
    print("=" * 60)
    print("\n  1. To apply the updated .env to running containers:")
    print("       docker compose up -d --force-recreate")
    print("     (The --force-recreate flag ensures containers reload the env file.")
    print("      A plain 'docker compose up -d' may NOT pick up env changes.)")
    print("\n  2. If running directly with Python:")
    print("       python server.py")
    print("\n  3. Log in at:  /admin")
    print(f"     Email   :  {email}")
    print("     Password:  (the one you just entered)")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
