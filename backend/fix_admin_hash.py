"""
fix_admin_hash.py
─────────────────
Run this from your backend/ folder:
    python fix_admin_hash.py

It generates a fresh bcrypt hash and writes it cleanly into
BOTH .env and .env.local — no Windows line ending corruption,
no truncation, no quotes.
"""
import os, sys, re, getpass

try:
    import bcrypt
except ImportError:
    os.system(f"{sys.executable} -m pip install bcrypt")
    import bcrypt

print("\n══════════════════════════════════════════")
print("   MeetFree — Fix Super Admin Hash")
print("══════════════════════════════════════════\n")

while True:
    email = input("Super Admin email: ").strip().lower()
    if "@" in email:
        break
    print("  Enter a valid email.")

while True:
    pw = getpass.getpass("Password (min 8 chars, hidden): ")
    if len(pw) < 8:
        print("  Too short.")
        continue
    pw2 = getpass.getpass("Confirm password: ")
    if pw != pw2:
        print("  Passwords do not match.")
        continue
    break

print("\n  Generating hash...")
# Generate clean hash — no trailing newline or whitespace
pw_hash = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8").strip()
print(f"  Hash: {pw_hash[:20]}...")

# Verify it works before saving
assert bcrypt.checkpw(pw.encode("utf-8"), pw_hash.encode("utf-8")), "Hash verification failed!"
print("  Hash verified ✓")

script_dir = os.path.dirname(os.path.abspath(__file__))

def write_env(path, email, pw_hash):
    """Write/update env file with correct values, Unix line endings."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            # Normalize all line endings to Unix \n
            lines = f.read().replace("\r\n", "\n").replace("\r", "\n").split("\n")
    else:
        lines = []

    def set_key(lines, key, value):
        """Replace or append a key=value line."""
        new_lines = []
        found = False
        for line in lines:
            if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                new_lines.append(f"{key}={value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}")
        return new_lines

    lines = set_key(lines, "SUPER_ADMIN_EMAIL",         email)
    lines = set_key(lines, "SUPER_ADMIN_PASSWORD_HASH", pw_hash)

    # Write with Unix line endings explicitly
    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

# Update every env file found
targets = [
    os.path.join(script_dir,        ".env"),
    os.path.join(script_dir,        ".env.local"),
    os.path.join(script_dir, "..", ".env"),
    os.path.join(script_dir, "..", ".env.local"),
]

print()
written = []
for path in targets:
    path = os.path.abspath(path)
    if path in written:
        continue
    if os.path.exists(path):
        write_env(path, email, pw_hash)
        written.append(path)
        print(f"  ✓ Updated: {path}")

if not written:
    # Create both if neither exists
    for name in [".env", ".env.local"]:
        path = os.path.abspath(os.path.join(script_dir, name))
        write_env(path, email, pw_hash)
        written.append(path)
        print(f"  ✓ Created: {path}")

print()
print("══════════════════════════════════════════")
print("  ✓  Done! Now restart:")
print()
print("  Python:  python server.py")
print("  Docker:  docker compose -f docker-compose.local.yml restart meetfree-app")
print("══════════════════════════════════════════\n")