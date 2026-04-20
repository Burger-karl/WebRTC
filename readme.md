# MeetFree — Self-Hosted WebRTC Video Conferencing

A production-ready, unlimited video conferencing platform built with Python + Flask-SocketIO + WebRTC.  
Inspired by Whereby. No third-party APIs. No time limits. Fully self-hosted.

---

## Branches

| Branch | Description | Docs |
|---|---|---|
| `main` | Production-ready build | This file |
| `feature-dev` | Persistent chat, room passwords, host controls | [FEATURE_DEV.md](./FEATURE_DEV.md) |


## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Project Structure](#3-project-structure)
4. [Environment Variables](#4-environment-variables)
5. [Running Locally (Without Docker)](#5-running-locally-without-docker)
6. [Running with Docker — Local Testing](#6-running-with-docker--local-testing)
7. [Deploying to Production (OCI / Any Cloud Server)](#7-deploying-to-production-oci--any-cloud-server)
8. [Single-Command Deployment](#8-single-command-deployment)
9. [Verifying the Deployment](#9-verifying-the-deployment)
10. [TURN Server Setup (Corporate Network Support)](#10-turn-server-setup-corporate-network-support)
11. [Feature Reference](#11-feature-reference)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Project Overview

MeetFree is a fully Dockerized, microservice-ready video conferencing backend.  
Your technical lead's requirements are addressed as follows:

| Requirement | Implementation |
|---|---|
| JWT Authentication | `backend/auth.py` — every socket connection is token-gated |
| Scalability (100+ users) | Redis pub/sub message queue across 3 Gunicorn workers |
| STUN/TURN for firewalls | ICE config served from server at join time, TURN credentials in `.env` |
| Full Docker orchestration | `docker-compose.yml` — single command brings up the entire stack |
| All env vars in `.env` | `backend/.env` controls every configurable value |
| Backend + Frontend orchestrated | Python app + Nginx reverse proxy in one Compose file |
| OCI-ready deployment | Standard Docker Compose — works on any Linux server including OCI |

---

## 2. Architecture

```
                        ┌─────────────────────────────────────┐
                        │         Docker Compose Stack         │
                        │                                      │
  Browser ─── HTTPS ──► │  Nginx (port 80/443)                 │
                        │    │                                 │
                        │    ├── Static files (app.js, CSS)    │
                        │    │                                 │
                        │    └── Proxy ──► MeetFree App        │
                        │                  (Gunicorn/eventlet) │
                        │                  3 workers           │
                        │                    │                 │
                        │                    └──► Redis        │
                        │                    (Socket.IO queue) │
                        └─────────────────────────────────────┘

WebRTC media (video/audio) flows DIRECTLY peer-to-peer between browsers.
The server only handles the signaling handshake — it never touches video data.
```

**Services:**

- **Nginx** — Reverse proxy. Handles HTTPS termination, WebSocket upgrade headers, and static file caching. Listens on ports 80 and 443.
- **MeetFree (Python/Gunicorn)** — Flask-SocketIO signaling server. Issues JWTs, relays WebRTC offers/answers/ICE candidates. 3 workers in production.
- **Redis** — Message queue that lets all 3 Gunicorn workers share Socket.IO events. Required for multi-worker mode.

---

## 3. Project Structure

```
prod-app/
├── Dockerfile                    # Python app container definition
├── docker-compose.yml            # Production: 3 workers, HTTPS, Redis
├── docker-compose.local.yml      # Local testing: 1 worker, HTTP only
│
├── backend/
│   ├── server.py                 # Flask app + all Socket.IO event handlers
│   ├── auth.py                   # JWT creation, verification, @require_auth
│   ├── config.py                 # Reads all .env vars, builds ICE server list
│   ├── requirements.txt          # Python dependencies
│   ├── .env.example              # Template — copy to .env for production
│   └── .env.local                # Pre-filled values for local testing
│
├── frontend/
│   ├── index.html                # Landing page (name + room entry, JWT fetch)
│   ├── room.html                 # Video call room UI
│   ├── app.js                    # WebRTC client logic, all socket events
│   └── style.css                 # Whereby-inspired dark UI
│
└── nginx/
    ├── nginx.conf                # Production config (HTTPS)
    └── nginx.local.conf          # Local config (HTTP only)
```

---

## 4. Environment Variables

All configuration is done through a single `.env` file in the `backend/` directory.  
**Never commit this file to version control.**

Copy the template to get started:

```bash
# For local testing
cp backend/.env.local backend/.env.local     # already exists, ready to use

# For production
cp backend/.env.example backend/.env         # then fill in real values
```

### Complete Variable Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | **Yes** | — | Flask session signing key. Use a long random string. |
| `JWT_SECRET` | **Yes** | — | JWT signing key. Keep private. Rotate to invalidate all active sessions. |
| `JWT_EXPIRY_SECONDS` | No | `28800` | How long a token is valid (default: 8 hours). |
| `REDIS_URL` | No | _(none)_ | Redis connection string. Required for multi-worker mode. In Docker Compose this is auto-set to `redis://redis:6379/0`. |
| `MAX_USERS_PER_ROOM` | No | `100` | Hard cap on participants per room. |
| `ALLOWED_ORIGINS` | No | `*` | CORS allowed origins. Set to your domain in production: `https://yourdomain.com`. |
| `STUN_URLS` | No | Google STUN | Comma-separated STUN server URLs. |
| `TURN_URL` | No | _(none)_ | TURN server URL e.g. `turn:turn.yourdomain.com:3478`. Required for users on corporate firewalls. |
| `TURN_USERNAME` | No | _(none)_ | TURN server username. |
| `TURN_CREDENTIAL` | No | _(none)_ | TURN server password. |
| `RATE_LIMIT_PER_MINUTE` | No | `20` | Max `/api/token` requests per IP per minute. |
| `PORT` | No | `5000` | Internal Python server port (Nginx proxies to this). |

> **Note:** In Docker Compose, `REDIS_URL` and `PYTHONPATH` are always injected by the Compose file itself — you do not need to set them in `.env`.

---

## 5. Running Locally (Without Docker)

Use this method for quick development and debugging without containers.

### Prerequisites
- Python 3.10 or higher
- pip

### Steps

```bash
# 1. Navigate into the backend folder
cd backend

# 2. Create and activate a virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
#    The .env.local file is pre-filled with safe local values
copy .env.local .env        # Windows
cp .env.local .env          # macOS / Linux

# 5. Start the server
python server.py
```

Open your browser at: **http://localhost:5000**

> **Note:** Running without Docker means no Redis. The server falls back to in-memory mode automatically — this works fine but only supports a single process. Suitable for local testing only.

---

## 6. Running with Docker — Local Testing

This is the recommended way to test the full production stack on your machine.  
It runs all three services (Python app + Redis + Nginx) with a single command.

### Prerequisites
- Docker Desktop installed and running
- No other service using ports 80 or 5000 on your machine

### Steps

**Step 1 — Confirm Docker is running**
```bash
docker --version
docker compose version
```

Both commands should print version numbers. If not, open Docker Desktop and wait for it to fully start.

**Step 2 — Navigate to the project root**
```bash
cd prod-app
```

**Step 3 — Build and start all services**
```bash
docker compose -f docker-compose.local.yml up --build
```

The first run takes 2–4 minutes as Docker downloads base images and installs Python packages. Subsequent starts are much faster.

**Step 4 — Confirm all services are healthy**

Watch the logs for this output from each service:

```
redis-1     | Ready to accept connections
meetfree-1  | [INFO] Listening at: http://0.0.0.0:5000
nginx-1     | /docker-entrypoint.sh: Configuration complete
```

**Step 5 — Open the app**

Go to: **http://localhost**

**Step 6 — Test a call with two users**

1. Open **http://localhost** in your browser — enter name `Alice`, room `test-room`, click Join
2. Open **http://localhost** in a second browser tab or incognito window — enter name `Bob`, same room `test-room`, click Join
3. Both tabs should connect and show each other's video

### Local Testing Quick-Reference Commands

```bash
# Start (first time or after code changes to Dockerfile/requirements)
docker compose -f docker-compose.local.yml up --build

# Start (after code changes to .py or .js files only — no rebuild needed)
docker compose -f docker-compose.local.yml up

# View live logs from all services
docker compose -f docker-compose.local.yml logs -f

# View logs from the Python app only
docker compose -f docker-compose.local.yml logs -f meetfree

# Stop all containers (keeps data)
docker compose -f docker-compose.local.yml down

# Stop and wipe all data (Redis state, volumes)
docker compose -f docker-compose.local.yml down -v

# Check container status
docker compose -f docker-compose.local.yml ps

# Open a shell inside the running app container
docker exec -it prod-app-meetfree-1 /bin/sh
```

### What the Local Compose File Does Differently from Production

| Setting | Local (`docker-compose.local.yml`) | Production (`docker-compose.yml`) |
|---|---|---|
| Port | HTTP only (port 80) | HTTP + HTTPS (80 + 443) |
| SSL | None needed | Requires certs in `nginx/certs/` |
| Gunicorn workers | 1 (easier log reading) | 3 (handles concurrent load) |
| Log level | `debug` (verbose) | `info` (clean) |
| Gunicorn reload | `--reload` (auto-restarts on code change) | Off |
| Source mount | Live volume mount (edit files, refresh browser) | Baked into image |
| Redis memory | 128MB | 256MB |
| Redis port | Exposed on `6379` (inspect with redis-cli) | Internal only |
| App port | Also exposed on `5000` (bypass Nginx for debugging) | Internal only |

---

## 7. Deploying to Production (OCI / Any Cloud Server)

This section covers deploying MeetFree to an Oracle Cloud Infrastructure (OCI) instance or any Ubuntu/Debian server.

### Server Requirements

- Ubuntu 22.04 LTS (recommended) or any Debian-based OS
- Minimum: 2 vCPU, 4GB RAM (OCI `VM.Standard.E4.Flex` with 2 OCPU / 4GB works well)
- Ports open in OCI Security List: **22** (SSH), **80** (HTTP), **443** (HTTPS)
- A domain name pointed at the server's public IP

### Step 1 — SSH into your OCI Instance

```bash
ssh -i ~/your-key.pem ubuntu@<your-oci-public-ip>
```

### Step 2 — Install Docker on the Server

```bash
# Update packages
sudo apt update && sudo apt upgrade -y

# Install Docker
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
                    docker-buildx-plugin docker-compose-plugin

# Allow running Docker without sudo
sudo usermod -aG docker $USER
newgrp docker

# Verify
docker --version
docker compose version
```

### Step 3 — Upload the Project to the Server

**Option A — Git (recommended)**
```bash
# On your server
git clone https://github.com/your-org/meetfree.git
cd meetfree
```

**Option B — SCP from your local machine**
```bash
# Run this on your LOCAL machine
scp -i ~/your-key.pem -r ./prod-app ubuntu@<your-oci-ip>:~/meetfree
```

Then on the server:
```bash
cd ~/meetfree
```

### Step 4 — Get SSL Certificates

HTTPS is required in production — browsers block camera/mic access on non-HTTPS origins.

```bash
# Install Certbot
sudo apt install -y certbot

# Stop anything using port 80 temporarily
sudo docker compose down 2>/dev/null || true

# Get certificate (replace with your actual domain)
sudo certbot certonly --standalone -d yourdomain.com

# Certificates are saved to /etc/letsencrypt/live/yourdomain.com/
```

Copy the certificates into the project:
```bash
sudo mkdir -p nginx/certs
sudo cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem nginx/certs/
sudo cp /etc/letsencrypt/live/yourdomain.com/privkey.pem nginx/certs/
sudo chmod 644 nginx/certs/*.pem
```

### Step 5 — Configure Environment Variables

```bash
cp backend/.env.example backend/.env
nano backend/.env
```

Fill in these values at minimum:

```bash
# Generate strong secrets (run these commands to get random values)
openssl rand -hex 32    # use output as SECRET_KEY
openssl rand -hex 32    # use output as JWT_SECRET
```

```env
SECRET_KEY=<paste generated value here>
JWT_SECRET=<paste generated value here>
JWT_EXPIRY_SECONDS=28800
MAX_USERS_PER_ROOM=100
ALLOWED_ORIGINS=https://yourdomain.com
STUN_URLS=stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302

# TURN — leave blank if not configured yet
TURN_URL=
TURN_USERNAME=
TURN_CREDENTIAL=

RATE_LIMIT_PER_MINUTE=20
PORT=5000
```

Save and exit (`Ctrl+X`, `Y`, `Enter` in nano).

### Step 6 — Update Nginx Config with Your Domain

```bash
nano nginx/nginx.conf
```

Find this line and replace `yourdomain.com` with your actual domain:

```nginx
server_name yourdomain.com;
```

Save and exit.

### Step 7 — Deploy

```bash
docker compose up -d --build
```

That's it. The entire stack starts in the background.

---

## 8. Single-Command Deployment

Once Steps 1–6 are done (server prepared, certs in place, `.env` filled), the entire application deploys with:

```bash
docker compose up -d --build
```

To update after a code change:

```bash
git pull
docker compose up -d --build
```

Docker rebuilds only the layers that changed, so updates are fast.

---

## 9. Verifying the Deployment

### Health Check
```bash
curl https://yourdomain.com/health
```
Expected response:
```json
{
  "status": "ok",
  "active_rooms": 0,
  "active_peers": 0,
  "redis": true
}
```

`"redis": true` confirms Socket.IO is using Redis for multi-worker event sharing.

### Container Status
```bash
docker compose ps
```
All three services should show `Up` and `healthy`:
```
NAME                STATUS          PORTS
prod-app-redis-1    Up (healthy)    6379/tcp
prod-app-meetfree-1 Up (healthy)    5000/tcp
prod-app-nginx-1    Up              0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
```

### Live Logs
```bash
# All services
docker compose logs -f

# Python app only
docker compose logs -f meetfree
```

### JWT Token Test
```bash
curl -X POST https://yourdomain.com/api/token \
  -H "Content-Type: application/json" \
  -d '{"name": "testuser", "room": "test-room"}'
```
Expected response:
```json
{
  "token": "eyJ...",
  "iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}],
  "expiresIn": 28800
}
```

### SSL Certificate Check
```bash
curl -vI https://yourdomain.com 2>&1 | grep "SSL certificate"
```

---

## 10. TURN Server Setup (Corporate Network Support)

STUN servers reveal a peer's public IP, but on strict corporate or university firewalls, direct peer-to-peer connections are blocked entirely. A TURN server acts as a media relay in those cases — video flows through it rather than peer-to-peer.

Without TURN: calls may fail silently for users on restrictive networks.  
With TURN: calls work on virtually any network.

### Install coturn (open source TURN server)

Run this on your OCI server (or a separate server):

```bash
sudo apt install -y coturn
```

### Configure coturn

```bash
sudo nano /etc/turnserver.conf
```

Add these lines (replace with your values):
```
listening-port=3478
tls-listening-port=5349
fingerprint
lt-cred-mech
realm=yourdomain.com
user=meetfree:your-strong-password
total-quota=200
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
```

```bash
sudo systemctl enable coturn
sudo systemctl start coturn
```

Open port 3478 (UDP + TCP) in your OCI Security List.

### Add TURN to your `.env`

```env
TURN_URL=turn:yourdomain.com:3478
TURN_USERNAME=meetfree
TURN_CREDENTIAL=your-strong-password
```

Then redeploy:
```bash
docker compose up -d --build
```

TURN credentials are sent to the browser inside the `/api/token` response — they are never hard-coded in the frontend JavaScript.

### SSL Renewal (Let's Encrypt)

Certificates expire every 90 days. Set up auto-renewal:

```bash
# Test renewal
sudo certbot renew --dry-run

# Add a cron job to auto-renew
echo "0 3 * * * root certbot renew --quiet && \
  cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem /home/ubuntu/meetfree/nginx/certs/ && \
  cp /etc/letsencrypt/live/yourdomain.com/privkey.pem /home/ubuntu/meetfree/nginx/certs/ && \
  docker compose -f /home/ubuntu/meetfree/docker-compose.yml restart nginx" \
  | sudo tee /etc/cron.d/certbot-meetfree
```

---

## 11. Feature Reference

### Production Features

| Feature | Details |
|---|---|
| JWT Authentication | Room-scoped tokens. A token for `room-a` cannot join `room-b`. Issued at `/api/token`. |
| Redis Scalability | Socket.IO events shared across all Gunicorn workers via Redis pub/sub. |
| STUN/TURN from server | ICE credentials sent to browser at token time — never in frontend JS. |
| Room capacity cap | Configurable via `MAX_USERS_PER_ROOM` (default 100). |
| Rate limiting | `/api/token` limited per IP. Configurable via `RATE_LIMIT_PER_MINUTE`. |
| Health endpoint | `GET /health` returns JSON status — use for load balancer probes. |
| Structured logging | All events logged with `[TAG]` format for log aggregator compatibility. |
| Non-root container | Docker container runs as user `meetfree` (uid 1000), not root. |

### UI/UX Features

| Feature | Keyboard Shortcut |
|---|---|
| Mute / unmute microphone | `M` |
| Camera on / off | `V` |
| Screen share (toggle) | `S` |
| Raise / lower hand | `H` |
| Open / close chat | `C` |
| Copy room invite link | Click 🔗 |
| Leave call | Click 📵 |
| Participant count | Live display in control bar |
| Connection quality | RTT-based indicator (good / fair / poor) |
| Remote mute indicator | 🔇 overlay on remote tile when peer mutes |
| Remote camera-off indicator | 🚫 overlay on remote tile when peer disables camera |
| Hand raise visible to all | ✋ animated badge on tile |
| Chat timestamps | Every message shows send time |
| Chat auto-opens | Panel opens automatically when a message arrives |

---

## 12. Troubleshooting

### `ModuleNotFoundError: No module named 'config'`
The `PYTHONPATH` environment variable is missing. The `docker-compose.yml` and `Dockerfile` both set `PYTHONPATH=/app/backend` — ensure you are using the latest versions of those files and run:
```bash
docker compose down && docker compose up -d --build
```

### `ImportError: cannot import name 'request' from 'flask_socketio'`
You have an older version of `server.py` or `auth.py`. The fix was applied in the latest versions — both files import `request` from `flask` (not `flask_socketio`). Replace both files and restart.

### "Authenticating…" screen stuck / never loads
The Socket.IO connection is failing. Check the browser console (F12) for the error. Common causes:
- No token in `sessionStorage` — go back to the landing page and re-enter your name and room
- Server not running — check `docker compose ps` and `docker compose logs meetfree`

### Camera not working
Browsers require HTTPS for camera access on any origin other than `localhost`. If you are testing on a server IP address directly (e.g. `http://192.168.1.5`), camera will be blocked. Options:
- Use `http://localhost` for local testing
- Set up HTTPS with a real domain for remote access

### Port 80 already in use
```bash
# Find what is using port 80
sudo lsof -i :80

# Change the port in docker-compose.local.yml:
# ports: - "8080:80"
# Then open http://localhost:8080
```

### Redis connection errors
```bash
# Check Redis container is healthy
docker compose ps redis

# Ping Redis manually
docker exec -it prod-app-redis-1 redis-cli ping
# Expected: PONG
```

### Rebuild from scratch (fixes most Docker issues)
```bash
docker compose down -v
docker system prune -f
docker compose up --build
```