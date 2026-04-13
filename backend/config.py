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
    # Test keys start with sk_test_ / pk_test_
    # Live keys start with sk_live_ / pk_live_
    # Get yours at: https://dashboard.stripe.com/apikeys
    STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET:  str = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    # Stripe Price IDs — created in your Stripe Dashboard under Products
    # These are the IDs of the recurring prices you create (e.g. price_xxx)
    STRIPE_PRICE_MONTHLY: str = os.getenv("STRIPE_PRICE_MONTHLY", "")
    STRIPE_PRICE_YEARLY:  str = os.getenv("STRIPE_PRICE_YEARLY", "")

    # Your public domain — used to build Stripe redirect URLs
    # e.g. https://yourdomain.com  (no trailing slash)
    APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:5000")


cfg = Config()