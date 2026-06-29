"""
config.py — Centralised configuration for MeetFree.

FIXES APPLIED (from Team Lead Code Review):
  FIX 1 [config.py] Remove Critical Fallbacks: SECRET_KEY, JWT_SECRET, and
         ADMIN_SECRET_KEY no longer silently fall back to insecure dev strings
         in production. If FLASK_ENV=production and these vars are missing,
         the server raises an explicit RuntimeError at startup.

  FIX 2 [config.py] WebRTC TURN List Format: ice_servers() now always wraps
         TURN URL in a list ([self.TURN_URL]), ensuring cross-browser
         compatibility and preventing media stream setup failures.
"""

import os
from dotenv import load_dotenv

load_dotenv()

_IS_PRODUCTION = os.getenv("FLASK_ENV", "production") != "development"


def _require_env(key: str, fallback: str, critical: bool = False) -> str:
    """
    FIX 1: Get env var. In production, raise if a critical key is missing
    instead of silently returning an insecure default.
    """
    value = os.getenv(key, "").strip()
    if not value:
        if critical and _IS_PRODUCTION:
            raise RuntimeError(
                f"[CONFIG] FATAL: Environment variable '{key}' is required in production "
                f"but is not set. Set it in your .env file and restart the server. "
                f"Refusing to start with an insecure default."
            )
        # Dev mode: use fallback with a clear warning
        if not value:
            import logging
            logging.getLogger("meetfree.config").warning(
                f"[CONFIG] '{key}' not set — using insecure dev default. "
                f"Set this in .env before deploying to production."
            )
            return fallback
    return value


class Config:
    # ── Flask ─────────────────────────────────────────────────
    # FIX 1: SECRET_KEY is critical — raise in production if missing
    SECRET_KEY: str  = _require_env(
        "SECRET_KEY",
        fallback="dev-secret-change-in-production",
        critical=True,
    )
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
    # FIX 1: JWT_SECRET is critical — raise in production if missing
    JWT_SECRET:         str = _require_env(
        "JWT_SECRET",
        fallback="dev-jwt-secret-change-in-production",
        critical=True,
    )
    JWT_EXPIRY_SECONDS: int = int(os.getenv("JWT_EXPIRY_SECONDS", 28800))

    # ── Admin JWT ─────────────────────────────────────────────
    # FIX 1: ADMIN_SECRET_KEY is critical — raise in production if missing
    ADMIN_SECRET_KEY:         str = _require_env(
        "ADMIN_SECRET_KEY",
        fallback="dev-admin-secret-change-in-production",
        critical=True,
    )
    ADMIN_JWT_EXPIRY_SECONDS: int = int(os.getenv("ADMIN_JWT_EXPIRY_SECONDS", 3600))

    # ── Super Admin credentials ───────────────────────────────
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
        """
        FIX 2: TURN URL is always wrapped in a list ([self.TURN_URL]).
        The WebRTC spec requires `urls` to be a sequence of strings.
        Passing a bare string (not a list) causes silent failures in Safari
        and Firefox, preventing media stream negotiation.
        """
        servers = []
        stun_list = [u.strip() for u in self.STUN_URLS.split(",") if u.strip()]
        if stun_list:
            servers.append({"urls": stun_list})
        if self.TURN_URL and self.TURN_USERNAME and self.TURN_CREDENTIAL:
            servers.append({
                # FIX 2: Always a list — never a bare string
                "urls":       [self.TURN_URL],
                "username":   self.TURN_USERNAME,
                "credential": self.TURN_CREDENTIAL,
            })
        return servers

    # ── Stripe ────────────────────────────────────────────────
    STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_SECRET_KEY:      str = os.getenv("STRIPE_SECRET_KEY", "")
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
