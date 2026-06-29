"""
docker/wait-for-mysql.py
────────────────────────
Called by docker-compose command: sh -c "until python /app/docker/wait-for-mysql.py ..."

Exits 0  when MySQL accepts a real connection as the meetfree user.
Exits 1  when it cannot connect (docker-compose retries every 3 seconds).

WHY A SEPARATE FILE:
  Inlining Python inside YAML breaks in two ways:
    1. YAML parses curly braces {} as mappings — f-strings like f'error: {e}'
       cause a YAML parse error before Docker even reads the file.
    2. The shell inside `sh -c "..."` expands $variables — any Python variable
       like os.getenv() that contains a $ gets clobbered by the shell.
  A separate .py file avoids both problems completely.
"""

import os
import sys

try:
    import pymysql
except ImportError:
    print("[wait-for-mysql] pymysql not installed", flush=True)
    sys.exit(1)

host     = os.environ.get("MYSQL_HOST",     "mysql")
port     = int(os.environ.get("MYSQL_PORT", "3306"))
user     = os.environ.get("MYSQL_USER",     "meetfree")
password = os.environ.get("MYSQL_PASSWORD", "")
database = os.environ.get("MYSQL_DATABASE", "meetfree")

try:
    conn = pymysql.connect(
        host            = host,
        port            = port,
        user            = user,
        password        = password,
        database        = database,
        connect_timeout = 3,
    )
    conn.close()
    print("[wait-for-mysql] Connected successfully as " + user + "@" + host, flush=True)
    sys.exit(0)
except Exception as ex:
    print("[wait-for-mysql] Not ready: " + str(ex), flush=True)
    sys.exit(1)