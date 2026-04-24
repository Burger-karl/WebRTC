"""
config.py — Centralised configuration for MeetFree.
All environment variables are read here. Nothing else calls os.getenv().
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

    # ── JWT ───────────────────────────────────────────────────
    JWT_SECRET:         str = os.getenv("JWT_SECRET", "dev-jwt-secret-change-in-production")
    JWT_EXPIRY_SECONDS: int = int(os.getenv("JWT_EXPIRY_SECONDS", 28800))

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

    # ── MySQL (feature-dev branch) ────────────────────────────
    # Set MYSQL_ENABLED=false to run the app without a database.
    # Chat history, room passwords, and mute persistence are disabled
    # but signaling, video, and auth still work normally.
    MYSQL_ENABLED:  bool = os.getenv("MYSQL_ENABLED", "true").lower() == "true"
    MYSQL_HOST:     str  = os.getenv("MYSQL_HOST",     "localhost")
    MYSQL_PORT:     int  = int(os.getenv("MYSQL_PORT", 3306))
    MYSQL_USER:     str  = os.getenv("MYSQL_USER",     "meetfree")
    MYSQL_PASSWORD: str  = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE: str  = os.getenv("MYSQL_DATABASE", "meetfree")

    # How many chat messages to send to a user when they join a room
    CHAT_HISTORY_LIMIT: int = int(os.getenv("CHAT_HISTORY_LIMIT", 50))

    # ── Order Booking & Meeting Scheduling ────────────────────────
    # Stripe Price ID for a one-time service order.
    # Create a one-time price in your Stripe Dashboard under Products.
    STRIPE_SERVICE_PRICE_ID: str = os.getenv("STRIPE_SERVICE_PRICE_ID", "")

    # Default meeting duration shown to clients (informational only)
    MEETING_DURATION_MINUTES: int = int(os.getenv("MEETING_DURATION_MINUTES", 60))

    # Hours after payment to auto-schedule the first meeting slot
    MEETING_SCHEDULE_HOURS_AFTER: int = int(os.getenv("MEETING_SCHEDULE_HOURS_AFTER", 24))


cfg = Config()