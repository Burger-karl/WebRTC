"""
create_superadmin.py — One-time script to set up your Super Admin account.

Run from your backend/ folder:
    python create_superadmin.py

It will:
  1. Ask for email and password
  2. Generate the bcrypt hash
  3. Write SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD_HASH into
     BOTH .env AND .env.local so it works with:
       - python server.py  (reads .env)
       - Docker            (reads .env.local)
"""

import os
import sys
import re
import getpass

try:
    import bcrypt
except ImportError:
    print("\n[!] bcrypt not installed. Running: pip install bcrypt\n")
    os.system(f"{sys.executable} -m pip install bcrypt")
    import bcrypt

print()
print("══════════════════════════════════════════")
print("   MeetFree — Super Admin Account Setup   ")
print("══════════════════════════════════════════")
print()

while True:
    email = input("Enter Super Admin email: ").strip().lower()
    if email and "@" in email and "." in email:
        break
    print("  ✗  Please enter a valid email address.")

while True:
    password = getpass.getpass("Enter Super Admin password (hidden): ").strip()
    if len(password) < 8:
        print("  ✗  Password must be at least 8 characters.")
        continue
    confirm = getpass.getpass("Confirm password (hidden): ").strip()
    if password != confirm:
        print("  ✗  Passwords do not match. Try again.")
        continue
    break

print("\n  Generating bcrypt hash...")
pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
print("  ✓  Hash generated.")

script_dir = os.path.dirname(os.path.abspath(__file__))

# ── Find all env files to update ──────────────────────────────────────────────
# Looks for .env and .env.local in both backend/ and project root
candidates = [
    os.path.join(script_dir,        ".env"),
    os.path.join(script_dir,        ".env.local"),
    os.path.join(script_dir, "..", ".env"),
    os.path.join(script_dir, "..", ".env.local"),
]

# Deduplicate resolved paths
seen   = set()
unique = []
for p in candidates:
    resolved = os.path.abspath(p)
    if resolved not in seen:
        seen.add(resolved)
        unique.append(resolved)

def update_env_file(path, email, pw_hash):
    """Write SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD_HASH into an env file.
    Creates the file if it doesn't exist."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = ""

    # Update or insert SUPER_ADMIN_EMAIL
    if re.search(r"^SUPER_ADMIN_EMAIL\s*=", content, re.MULTILINE):
        content = re.sub(
            r"^SUPER_ADMIN_EMAIL\s*=.*$",
            f"SUPER_ADMIN_EMAIL={email}",
            content, flags=re.MULTILINE,
        )
    else:
        content += f"\nSUPER_ADMIN_EMAIL={email}\n"

    # Update or insert SUPER_ADMIN_PASSWORD_HASH
    if re.search(r"^SUPER_ADMIN_PASSWORD_HASH\s*=", content, re.MULTILINE):
        content = re.sub(
            r"^SUPER_ADMIN_PASSWORD_HASH\s*=.*$",
            f"SUPER_ADMIN_PASSWORD_HASH={pw_hash}",
            content, flags=re.MULTILINE,
        )
    else:
        content += f"SUPER_ADMIN_PASSWORD_HASH={pw_hash}\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return True

print()
updated = []
for path in unique:
    if os.path.exists(path):
        update_env_file(path, email, pw_hash)
        updated.append(path)
        print(f"  ✓  Updated: {path}")

# If neither .env nor .env.local exist anywhere, create both
if not updated:
    for filename in [".env", ".env.local"]:
        path = os.path.join(script_dir, filename)
        update_env_file(path, email, pw_hash)
        print(f"  ✓  Created: {path}")

print()
print("══════════════════════════════════════════")
print("  ✓  Super Admin account saved!")
print("══════════════════════════════════════════")
print(f"\n  Email    : {email}")
print(f"  Password : {'*' * len(password)}")
print()
print("  Next steps:")
print("  ┌─ Running with Python directly:")
print("  │    python server.py")
print("  │    → visit http://localhost:5000/admin")
print("  │")
print("  └─ Running with Docker:")
print("       docker compose -f docker-compose.local.yml restart meetfree-app")
print("       → visit http://localhost/admin")
print()