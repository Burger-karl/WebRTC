



"""
config.py — Centralised configuration for MeetFree.
All environment variables are read here. Nothing else calls os.getenv().

NEW in RBAC branch:
  ADMIN_SECRET_KEY          — signs admin JWTs (separate from user JWTs)
  SUPER_ADMIN_EMAIL         — super admin login email
  SUPER_ADMIN_PASSWORD_HASH — bcrypt hash of super admin password
  ADMIN_JWT_EXPIRY_SECONDS  — admin token lifetime (default 3600 = 1 hour)
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Flask ─────────────────────────────────────────────────
    SECRET_KEY: str  = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
    PORT:       int  = int(os.getenv("PORT", 5000))
    DEBUG:      bool = os.getenv("FLASK_ENV", "production") == "development"

    ALLOWED_ORIGINS: str = os.getenv("ALLOWED_ORIGINS", "*")

    @property
    def cors_origins(self):
        raw = self.ALLOWED_ORIGINS
        if raw == "*":
            return "*"
        return [o.strip() for o in raw.split(",") if o.strip()]

    # ── Redis ─────────────────────────────────────────────────
    REDIS_URL: str = os.getenv("REDIS_URL")

    @property
    def socketio_message_queue(self):
        return self.REDIS_URL if self.REDIS_URL else None

    # ── JWT (user tokens) ─────────────────────────────────────
    JWT_SECRET:         str = os.getenv("JWT_SECRET", "dev-jwt-secret-change-in-production")
    JWT_EXPIRY_SECONDS: int = int(os.getenv("JWT_EXPIRY_SECONDS", 28800))

    # ── Admin JWT (separate secret from user JWTs) ────────────
    # IMPORTANT: Set ADMIN_SECRET_KEY to a long random string in production.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    ADMIN_SECRET_KEY:         str = os.getenv("ADMIN_SECRET_KEY", "dev-admin-secret-change-in-production")
    ADMIN_JWT_EXPIRY_SECONDS: int = int(os.getenv("ADMIN_JWT_EXPIRY_SECONDS", 3600))

    # ── Super Admin credentials ───────────────────────────────
    # Generate hash with:
    #   python -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt(12)).decode())"
    SUPER_ADMIN_EMAIL:         str = os.getenv("SUPER_ADMIN_EMAIL", "")
    SUPER_ADMIN_PASSWORD_HASH: str = os.getenv("SUPER_ADMIN_PASSWORD_HASH", "")

    # ── Room limits ───────────────────────────────────────────
    MAX_USERS_PER_ROOM: int = int(os.getenv("MAX_USERS_PER_ROOM", 100))

    # ── Rate limiting ─────────────────────────────────────────
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", 20))

    # ── ICE / STUN / TURN ─────────────────────────────────────
    TURN_URL:        str = os.getenv("TURN_URL")
    TURN_USERNAME:   str = os.getenv("TURN_USERNAME")
    TURN_CREDENTIAL: str = os.getenv("TURN_CREDENTIAL")
    STUN_URLS:       str = os.getenv(
        "STUN_URLS",
        "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
    )

    def ice_servers(self) -> list:
        servers = []
        stun_list = [u.strip() for u in self.STUN_URLS.split(",") if u.strip()]
        if stun_list:
            servers.append({"urls": stun_list})
        if self.TURN_URL and self.TURN_USERNAME and self.TURN_CREDENTIAL:
            servers.append({
                "urls":       self.TURN_URL,
                "username":   self.TURN_USERNAME,
                "credential": self.TURN_CREDENTIAL,
            })
        return servers

    # ── Stripe ────────────────────────────────────────────────
    STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET:  str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_PRICE_MONTHLY:   str = os.getenv("STRIPE_PRICE_MONTHLY", "")
    STRIPE_PRICE_YEARLY:    str = os.getenv("STRIPE_PRICE_YEARLY", "")
    APP_BASE_URL:           str = os.getenv("APP_BASE_URL", "http://localhost:5000")

    # ── MySQL ─────────────────────────────────────────────────
    MYSQL_ENABLED:  bool = os.getenv("MYSQL_ENABLED", "true").lower() == "true"
    MYSQL_HOST:     str  = os.getenv("MYSQL_HOST",     "localhost")
    MYSQL_PORT:     int  = int(os.getenv("MYSQL_PORT", 3306))
    MYSQL_USER:     str  = os.getenv("MYSQL_USER",     "meetfree")
    MYSQL_PASSWORD: str  = os.getenv("MYSQL_PASSWORD", "meetfree123")
    MYSQL_DATABASE: str  = os.getenv("MYSQL_DATABASE", "meetfree")

    CHAT_HISTORY_LIMIT: int = int(os.getenv("CHAT_HISTORY_LIMIT", 50))

    # ── Order Booking ─────────────────────────────────────────
    STRIPE_SERVICE_PRICE_ID:       str = os.getenv("STRIPE_SERVICE_PRICE_ID", "")
    MEETING_DURATION_MINUTES:      int = int(os.getenv("MEETING_DURATION_MINUTES", 60))
    MEETING_SCHEDULE_HOURS_AFTER:  int = int(os.getenv("MEETING_SCHEDULE_HOURS_AFTER", 24))


cfg = Config()
