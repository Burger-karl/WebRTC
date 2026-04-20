# MeetFree — feature-dev Branch

> **Branch:** `feature-dev` — under review, not yet merged into `main`  
> **Base branch:** `main`  
> **Purpose:** Three new features added on top of the production-ready `main` build.

---

## What's New in This Branch

| # | Feature | What it does |
|---|---|---|
| 1 | **Persistent Chat** | Chat messages are saved to MySQL. History loads when you join a room, survives page refresh. |
| 2 | **Room Passwords** | Hosts can set a bcrypt-protected password when creating a room. Password field appears automatically for protected rooms. |
| 3 | **Host Controls — Remote Mute** | Host can force-mute any participant. Mute state is persisted to MySQL so late joiners see correct indicators. Kick (remove) was already in `main` — retained here. |

All three features **degrade gracefully** — if MySQL is unavailable or disabled, the app falls back to the `main` behaviour (video, signaling, lobby, JWT auth all continue working normally).

---

## Table of Contents

1. [New Files in This Branch](#1-new-files-in-this-branch)
2. [Prerequisites](#2-prerequisites)
3. [Environment Variables — Complete Reference](#3-environment-variables--complete-reference)
4. [Quick Start — Without MySQL (Fastest)](#4-quick-start--without-mysql-fastest)
5. [Quick Start — With Docker and MySQL (Full Features)](#5-quick-start--with-docker-and-mysql-full-features)
6. [Installing MySQL Manually (Without Docker)](#6-installing-mysql-manually-without-docker)
7. [Connecting to MySQL — All Options](#7-connecting-to-mysql--all-options)
8. [Database Schema](#8-database-schema)
9. [Running the Tests](#9-running-the-tests)
10. [Manual Testing Checklist](#10-manual-testing-checklist)
11. [Verifying the Database](#11-verifying-the-database)
12. [Architecture Changes](#12-architecture-changes)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. New Files in This Branch

```
prod-app/
├── backend/
│   ├── database.py          ← NEW  — all MySQL operations and schema
│   ├── server.py            ← MODIFIED — wired into DB, mute/unmute events
│   ├── config.py            ← MODIFIED — MYSQL_* variables added
│   ├── requirements.txt     ← MODIFIED — PyMySQL, bcrypt added
│   ├── .env.example         ← MODIFIED — MySQL section added
│   ├── .env.local           ← MODIFIED — MySQL defaults for Docker testing
│   └── test_features.py     ← NEW  — 20 individual feature tests
├── frontend/
│   ├── index.html           ← MODIFIED — password field, auto room check
│   ├── room.html            ← MODIFIED — chat history banner, host mute buttons
│   ├── app.js               ← MODIFIED — history load, mute events, host-mute lock
│   └── style.css            ← MODIFIED — password field, history, host controls UI
├── docker-compose.local.yml ← MODIFIED — MySQL service added
└── README.md                ← THIS FILE
```

Files **not changed** from `main`: `auth.py`, `payments.py`, `Dockerfile`,
`docker-compose.yml` (production), `nginx/nginx.conf`, `nginx/nginx.local.conf`

---

## 2. Prerequisites

| Tool | Minimum version | Required for |
|---|---|---|
| Python | 3.10+ | Running without Docker |
| pip | Any recent | Installing dependencies |
| Docker Desktop | 20+ | Running with Docker |
| MySQL | 8.0+ | Full feature testing (or use Docker — no install needed) |
| Git | Any | Branching and pushing |

---

## 3. Environment Variables — Complete Reference

All configuration lives in a single `.env` file inside the `backend/` folder.

### How to create your `.env` file

```bash
# Copy the template — then edit it
cp backend/.env.example backend/.env
```

> **Never commit `.env` to Git.** It is already in `.gitignore`.  
> The `.env.example` file is safe to commit — it contains no real secrets.

---

### Section 1 — Flask Core (required)

```env
SECRET_KEY=your-long-random-string-here
JWT_SECRET=another-long-random-string-here
JWT_EXPIRY_SECONDS=28800
PORT=5000
FLASK_ENV=production
```

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | **Yes** | Flask session signing key. Generate with: `openssl rand -hex 32` |
| `JWT_SECRET` | **Yes** | Signs every JWT token. Keep private. Rotate to invalidate all active sessions. |
| `JWT_EXPIRY_SECONDS` | No | How long a token is valid. Default: `28800` (8 hours) |
| `PORT` | No | Internal Python server port. Default: `5000` |
| `FLASK_ENV` | No | Set to `development` for verbose errors and auto-reload. Default: `production` |

---

### Section 2 — MySQL (new in this branch)

```env
MYSQL_ENABLED=true
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=meetfree
MYSQL_PASSWORD=your-mysql-password-here
MYSQL_DATABASE=meetfree
CHAT_HISTORY_LIMIT=50
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `MYSQL_ENABLED` | No | `true` | Set to `false` to disable all DB features. App still works — chat history, passwords, and mute persistence are just turned off. |
| `MYSQL_HOST` | Yes (if enabled) | `localhost` | Hostname or IP of your MySQL server. Use `mysql` if connecting to the Docker service. |
| `MYSQL_PORT` | No | `3306` | MySQL port. Only change if you run MySQL on a non-standard port. |
| `MYSQL_USER` | Yes (if enabled) | — | MySQL username. The user must have CREATE, INSERT, SELECT, DELETE privileges on `MYSQL_DATABASE`. |
| `MYSQL_PASSWORD` | Yes (if enabled) | — | Password for `MYSQL_USER`. Never use a blank password in production. |
| `MYSQL_DATABASE` | No | `meetfree` | Name of the MySQL database. Created automatically if using Docker Compose. |
| `CHAT_HISTORY_LIMIT` | No | `50` | How many previous messages to send to a user when they join a room. |

> **Tables are created automatically** on first startup. You do not need to run any SQL migration scripts manually.

---

### Section 3 — Redis

```env
REDIS_URL=redis://localhost:6379/0
```

| Variable | Required | Description |
|---|---|---|
| `REDIS_URL` | No | Leave blank for single-process mode (fine for local dev). Required for multi-worker production. In Docker Compose, this is auto-set to `redis://redis:6379/0`. |

---

### Section 4 — CORS and Domain

```env
ALLOWED_ORIGINS=https://yourdomain.com
APP_BASE_URL=https://yourdomain.com
```

| Variable | Required | Description |
|---|---|---|
| `ALLOWED_ORIGINS` | No | Comma-separated list of allowed CORS origins. Use `*` for local dev. |
| `APP_BASE_URL` | No | Your public domain. Used for Stripe redirect URLs. No trailing slash. |

---

### Section 5 — STUN / TURN

```env
STUN_URLS=stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302
TURN_URL=
TURN_USERNAME=
TURN_CREDENTIAL=
```

| Variable | Required | Description |
|---|---|---|
| `STUN_URLS` | No | Comma-separated STUN server URLs. Defaults to Google's free STUN servers. |
| `TURN_URL` | No | TURN relay server URL. Required for users on strict corporate networks. Example: `turn:turn.yourdomain.com:3478` |
| `TURN_USERNAME` | No | TURN server username. |
| `TURN_CREDENTIAL` | No | TURN server password. |

---

### Section 6 — Stripe Payments

```env
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_MONTHLY=price_...
STRIPE_PRICE_YEARLY=price_...
```

| Variable | Required | Description |
|---|---|---|
| `STRIPE_SECRET_KEY` | No | Leave blank to disable payments entirely. Get from [dashboard.stripe.com/test/apikeys](https://dashboard.stripe.com/test/apikeys) |
| `STRIPE_PUBLISHABLE_KEY` | No | Same page — publishable key. |
| `STRIPE_WEBHOOK_SECRET` | No | From Stripe Webhooks dashboard. Starts with `whsec_`. |
| `STRIPE_PRICE_MONTHLY` | No | Price ID for monthly plan. Create under Products in Stripe Dashboard. |
| `STRIPE_PRICE_YEARLY` | No | Price ID for yearly plan. |

---

### Section 7 — Room Limits

```env
MAX_USERS_PER_ROOM=100
RATE_LIMIT_PER_MINUTE=20
```

| Variable | Required | Description |
|---|---|---|
| `MAX_USERS_PER_ROOM` | No | Hard cap on participants per room. Default: `100` |
| `RATE_LIMIT_PER_MINUTE` | No | Max `/api/token` requests per IP per minute. Default: `20` |

---

### Complete `.env` example (local development)

```env
# Flask
SECRET_KEY=local-dev-secret-not-for-production
JWT_SECRET=local-jwt-secret-not-for-production
JWT_EXPIRY_SECONDS=28800
PORT=5000
FLASK_ENV=development
ALLOWED_ORIGINS=*
APP_BASE_URL=http://localhost:5000

# Redis (Docker sets this automatically — only needed for plain Python mode)
REDIS_URL=

# Room
MAX_USERS_PER_ROOM=100
RATE_LIMIT_PER_MINUTE=100
CHAT_HISTORY_LIMIT=50

# MySQL — change MYSQL_ENABLED to false to skip MySQL entirely
MYSQL_ENABLED=true
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=meetfree
MYSQL_PASSWORD=yourpassword
MYSQL_DATABASE=meetfree

# STUN / TURN
STUN_URLS=stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302
TURN_URL=
TURN_USERNAME=
TURN_CREDENTIAL=

# Stripe — leave blank to disable payments
STRIPE_SECRET_KEY=
STRIPE_PUBLISHABLE_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRICE_MONTHLY=
STRIPE_PRICE_YEARLY=
```

---

## 4. Quick Start — Without MySQL (Fastest)

Use this to test video, signaling, lobby, and host mute/kick. Chat history and room passwords are disabled but everything else works.

```bash
# 1. Navigate to the project
cd prod-app

# 2. Activate virtual environment
# Windows:
venv\Scripts\activate
# Mac / Linux:
source venv/bin/activate

# 3. Install new dependencies
pip install -r backend/requirements.txt

# 4. Create .env with MySQL disabled
cp backend/.env.example backend/.env
```

Open `backend/.env` and set:
```env
MYSQL_ENABLED=false
FLASK_ENV=development
ALLOWED_ORIGINS=*
```

```bash
# 5. Run the feature tests (no server or MySQL needed)
cd backend
python test_features.py
# Expected: 20 passed, 0 failed

# 6. Start the server
python server.py
```

Open **http://localhost:5000**

---

## 5. Quick Start — With Docker and MySQL (Full Features)

This runs all three new features completely. No MySQL installation needed — Docker handles it.

```bash
# 1. Make sure Docker Desktop is running (whale icon in taskbar/menu bar)

# 2. Navigate to project
cd prod-app

# 3. Your backend/.env.local already has correct Docker MySQL values.
#    No changes needed unless you want to customise.

# 4. Remove old containers and volumes (important — wipes stale MySQL data)
docker compose -f docker-compose.local.yml down -v

# 5. Build and start all services (Python app + Redis + MySQL)
docker compose -f docker-compose.local.yml up --build
```

Wait for all three ready signals:
```
mysql-1     | ready for connections
redis-1     | Ready to accept connections
meetfree-1  | [INFO] Listening at: http://0.0.0.0:5000
```

> MySQL takes ~30 seconds on first start. This is normal.

Open **http://localhost**

---

## 6. Installing MySQL Manually (Without Docker)

Only needed if you want to run `python server.py` directly (without Docker) AND test the MySQL features.

### Windows

1. Download MySQL Installer from **https://dev.mysql.com/downloads/installer/**
2. Run the installer — choose **Developer Default**
3. Set a root password when prompted — write it down
4. After install, open **MySQL Command Line Client** from Start menu and run:

```sql
CREATE DATABASE meetfree;
CREATE USER 'meetfree'@'localhost' IDENTIFIED BY 'your_chosen_password';
GRANT ALL PRIVILEGES ON meetfree.* TO 'meetfree'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

5. Update `backend/.env`:
```env
MYSQL_ENABLED=true
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=meetfree
MYSQL_PASSWORD=your_chosen_password
MYSQL_DATABASE=meetfree
```

### macOS

```bash
# Install via Homebrew
brew install mysql
brew services start mysql

# Secure the installation
mysql_secure_installation

# Create database and user
mysql -u root -p
```

```sql
CREATE DATABASE meetfree;
CREATE USER 'meetfree'@'localhost' IDENTIFIED BY 'your_chosen_password';
GRANT ALL PRIVILEGES ON meetfree.* TO 'meetfree'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

### Linux (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y mysql-server
sudo systemctl start mysql
sudo systemctl enable mysql

# Create database and user
sudo mysql
```

```sql
CREATE DATABASE meetfree;
CREATE USER 'meetfree'@'localhost' IDENTIFIED BY 'your_chosen_password';
GRANT ALL PRIVILEGES ON meetfree.* TO 'meetfree'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

> **Tables are created automatically** when the app starts. You only need to create the database and user.

---

## 7. Connecting to MySQL — All Options

The `MYSQL_HOST` value changes depending on how you're running MySQL:

| Setup | `MYSQL_HOST` value | Notes |
|---|---|---|
| Docker Compose (recommended) | `mysql` | The Docker service name — Docker resolves it automatically |
| MySQL installed on your laptop | `localhost` or `127.0.0.1` | Running `python server.py` directly |
| MySQL on another machine/VM | `192.168.1.x` or hostname | Must be reachable from your machine |
| MySQL on a cloud server (RDS, PlanetScale) | The cloud endpoint URL | Also update `MYSQL_PORT` if non-standard |

---

## 8. Database Schema

Tables are **created automatically** on first startup — you never need to run SQL manually.

### `chat_messages` — stores all room chat history

```sql
CREATE TABLE chat_messages (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    room_id     VARCHAR(80)  NOT NULL,
    sender_name VARCHAR(50)  NOT NULL,
    message     TEXT         NOT NULL,
    sent_at     DATETIME     DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_room_sent (room_id, sent_at)
);
```

### `rooms` — stores room records and bcrypt password hashes

```sql
CREATE TABLE rooms (
    room_id       VARCHAR(80)  PRIMARY KEY,
    password_hash VARCHAR(255) DEFAULT NULL,  -- NULL = open room, no password
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    created_by    VARCHAR(50)  NOT NULL
);
```

### `participant_mute_state` — host mute state per participant

```sql
CREATE TABLE participant_mute_state (
    room_id    VARCHAR(80) NOT NULL,
    peer_name  VARCHAR(50) NOT NULL,
    is_muted   TINYINT(1)  DEFAULT 0,
    updated_at DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (room_id, peer_name)
);
```

---

## 9. Running the Tests

Tests run in under 5 seconds with no server, no MySQL, and no browser required.
Everything is mocked internally.

```bash
cd backend

# Install test dependencies (only needed once)
pip install bcrypt PyMySQL

# Run all 20 feature tests
python test_features.py
```

### Expected output

```
────────────────────────────────────────────────────────────
  Feature 1 — Persistent Chat (MySQL)
────────────────────────────────────────────────────────────

  ✓ PASS  save_message() inserts with correct SQL and parameters
  ✓ PASS  save_message() truncates messages longer than 500 chars
  ✓ PASS  get_room_history() returns messages oldest-first
  ✓ PASS  save_message() returns None gracefully when DB is down
  ✓ PASS  get_room_history() returns [] when DB is down
  ✓ PASS  delete_room_history() cleans up both tables

────────────────────────────────────────────────────────────
  Feature 2 — Room Passwords
────────────────────────────────────────────────────────────

  ✓ PASS  create_room() stores bcrypt hash, not plaintext password
  ✓ PASS  create_room() stores NULL for open rooms (no password)
  ✓ PASS  verify_room_password() accepts the correct password
  ✓ PASS  verify_room_password() rejects the wrong password
  ✓ PASS  verify_room_password() allows entry to open rooms
  ✓ PASS  verify_room_password() allows entry when room has no DB record yet
  ✓ PASS  verify_room_password() fails open (allows entry) when DB is down
  ✓ PASS  create_room() treats empty string password as open room (NULL)

────────────────────────────────────────────────────────────
  Feature 3 — Host Controls (Mute / Kick)
────────────────────────────────────────────────────────────

  ✓ PASS  set_participant_muted() uses UPSERT (no duplicate rows)
  ✓ PASS  set_participant_muted() stores 0 for unmuted state
  ✓ PASS  get_muted_participants() returns list of muted names
  ✓ PASS  get_muted_participants() returns [] when DB is down
  ✓ PASS  Mute state: set then retrieve returns consistent data
  ✓ PASS  Only host can mute — server-side admin check

────────────────────────────────────────────────────────────
  Results: 20 passed, 0 failed / 20 total
────────────────────────────────────────────────────────────

All feature tests passed.
```

### Run with pytest (optional)

```bash
pip install pytest
pytest backend/test_features.py -v
```

---

## 10. Manual Testing Checklist

Use this checklist after starting the app. Open two browser tabs to the same room.

### Feature 1 — Persistent Chat

- [ ] Tab 1 joins `test-room`, sends 3 messages
- [ ] Tab 2 joins `test-room` — sees all 3 previous messages with "📜 Chat history loaded" banner
- [ ] Tab 1 refreshes — rejoins and sees history reload from MySQL
- [ ] Tab 2 sends a message — Tab 1 sees it in real time

### Feature 2 — Room Passwords

- [ ] Tab 1 joins a new room with password `secret123` — lands in room as host
- [ ] Tab 2 opens landing page, types the same room name — 🔒 password field appears automatically
- [ ] Tab 2 types `wrongpassword` → sees "Incorrect room password" error
- [ ] Tab 2 types `secret123` → joins the room successfully
- [ ] Open a third tab with a brand-new room name — "set password" field appears instead (for creating, not joining)

### Feature 3 — Host Controls

- [ ] Two tabs in the same room
- [ ] Tab 1 (host) hovers over Tab 2's video tile — Mute and Remove buttons appear
- [ ] Tab 1 clicks **🔇 Mute** — Tab 2 sees muted toast, mic button shows 🔐
- [ ] Tab 2 tries to click mic button — sees "The host has muted you" message
- [ ] A third tab joins — sees 🔇 indicator already on Tab 2's tile (from MySQL)
- [ ] Tab 1 clicks **Unmute** — Tab 2 sees unmuted toast and can re-enable mic
- [ ] Tab 1 clicks **✕ Remove** on Tab 2 — Tab 2 is kicked to the home page

---

## 11. Verifying the Database

After testing with Docker, inspect the MySQL tables directly:

```bash
# Open a MySQL shell inside the Docker container
docker exec -it prod-app-mysql-1 mysql -u meetfree -pmeetfree_local_password meetfree
```

```sql
-- See all saved chat messages
SELECT room_id, sender_name, message, sent_at FROM chat_messages;

-- See all rooms (shows open vs password-protected)
SELECT
    room_id,
    CASE WHEN password_hash IS NULL THEN 'open' ELSE 'password-protected' END AS type,
    created_by,
    created_at
FROM rooms;

-- See current mute states
SELECT room_id, peer_name, is_muted, updated_at FROM participant_mute_state;

-- Exit MySQL
EXIT;
```

---

## 12. Architecture Changes

New services and data flow added in this branch:

```
Before (main branch):
  Browser → Nginx → Python (Flask/SocketIO) → Redis
                           ↓
                      In-memory state only

After (feature-dev branch):
  Browser → Nginx → Python (Flask/SocketIO) → Redis
                           ↓
                         MySQL
                    ┌──────────────────────────┐
                    │ chat_messages            │ ← chat history
                    │ rooms                    │ ← room passwords
                    │ participant_mute_state   │ ← host mute state
                    └──────────────────────────┘
```

MySQL is **optional** — the app falls back gracefully if it's unavailable:

| Feature | MySQL available | MySQL disabled / down |
|---|---|---|
| Persistent chat | Messages saved, history loads on join | Messages broadcast in real time only (lost on refresh) |
| Room passwords | bcrypt-verified on every join | Password field hidden, all rooms open |
| Host mute persistence | Late joiners see correct mute state | Late joiners do not see existing mutes |
| Video / signaling | ✅ Works normally | ✅ Works normally |
| JWT auth | ✅ Works normally | ✅ Works normally |
| Lobby / waiting room | ✅ Works normally | ✅ Works normally |
| Stripe payments | ✅ Works normally | ✅ Works normally |

---

## 13. Troubleshooting

### `ModuleNotFoundError: No module named 'database'`
`PYTHONPATH` is not set. Ensure `docker-compose.local.yml` has:
```yaml
environment:
  PYTHONPATH: /app/backend
```
Then rebuild: `docker compose -f docker-compose.local.yml up --build`

### `ModuleNotFoundError: No module named 'PyMySQL'`
```bash
pip install PyMySQL bcrypt
# Or rebuild Docker: docker compose -f docker-compose.local.yml up --build
```

### `Can't connect to MySQL server` in logs
MySQL is not ready yet — it takes ~30 seconds on first run. Wait until you see `mysql-1 | ready for connections`. If it never appears:
```bash
docker compose -f docker-compose.local.yml logs mysql
```

### `Access denied for user 'meetfree'@...`
The MySQL user credentials in your `.env` or Docker Compose environment don't match what MySQL was initialised with. Fix by wiping the MySQL volume and restarting:
```bash
docker compose -f docker-compose.local.yml down -v
docker compose -f docker-compose.local.yml up --build
```

### Password field not appearing on the landing page
The `debounceRoomCheck()` call in `index.html` pings `/api/check-room`. Check:
- The server is running (check terminal or Docker logs)
- You typed a room name that is currently live (someone is in it)
- Open browser DevTools → Network tab → look for the `check-room` request and its response

### Chat history not loading
- Confirm `MYSQL_ENABLED=true` in your `.env`
- Confirm MySQL is connected: `curl http://localhost:5000/health` → `"mysql": true`
- Check Docker logs: `docker compose -f docker-compose.local.yml logs meetfree`

### `test_features.py` fails with `ModuleNotFoundError`
Run the tests from inside the `backend/` directory:
```bash
cd backend
python test_features.py
```

### Host mute buttons not visible
- Mute/Remove buttons only appear on **hover** over a remote participant's tile
- They only show for the **host** (first person to join the room)
- Confirm you are the host — you should have seen a "👑 You are the host" toast on joining

---

## Merging This Branch

When the lead dev approves, merge into `main` with:

```bash
git checkout main
git merge --no-ff feature-dev -m "merge: persistent chat, room passwords, host controls"
git push origin main
```

The `--no-ff` flag preserves the branch history in the Git log.