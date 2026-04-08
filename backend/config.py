
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Flask / SocketIO ──────────────────────────────────────
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
    PORT:       int = int(os.getenv("PORT", 5000))
    DEBUG:      bool = os.getenv("FLASK_ENV", "production") == "development"

    # CORS — "origins" param passed to Flask-SocketIO
    ALLOWED_ORIGINS: str | list = os.getenv("ALLOWED_ORIGINS", "*")

    # If ALLOWED_ORIGINS is comma-separated, turn it into a list
    @property
    def cors_origins(self) -> list | str:
        raw = self.ALLOWED_ORIGINS
        if raw == "*":
            return "*"
        return [o.strip() for o in raw.split(",") if o.strip()]

    # ── Redis (for multi-worker scalability) ─────────────────
    REDIS_URL: str | None = os.getenv("REDIS_URL")  # None → in-memory (single worker)

    @property
    def socketio_message_queue(self) -> str | None:
        """Return Redis URL for SocketIO, or None for single-process mode."""
        return self.REDIS_URL if self.REDIS_URL else None

    # ── JWT ──────────────────────────────────────────────────
    JWT_SECRET:          str = os.getenv("JWT_SECRET", "dev-jwt-secret-change-in-production")
    JWT_EXPIRY_SECONDS:  int = int(os.getenv("JWT_EXPIRY_SECONDS", 28800))  # 8 hours

    # ── Room limits ──────────────────────────────────────────
    MAX_USERS_PER_ROOM: int = int(os.getenv("MAX_USERS_PER_ROOM", 100))

    # ── Rate limiting ────────────────────────────────────────
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", 20))

    # ── ICE / STUN / TURN ────────────────────────────────────
    TURN_URL:        str | None = os.getenv("TURN_URL")
    TURN_USERNAME:   str | None = os.getenv("TURN_USERNAME")
    TURN_CREDENTIAL: str | None = os.getenv("TURN_CREDENTIAL")
    STUN_URLS:       str        = os.getenv(
        "STUN_URLS",
        "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
    )

    def ice_servers(self) -> list[dict]:
        """
        Build the iceServers list that gets sent to the browser.
        Includes all configured STUN servers, plus the TURN server if set.

        Why TURN matters:
            STUN tells a peer its public IP address (so the other side can
            reach it), but on restrictive corporate firewalls STUN often
            fails. A TURN server acts as a relay — media flows THROUGH it —
            guaranteeing connectivity at the cost of a bit of extra latency.
            Without TURN, calls on strict networks simply fail silently.
        """
        servers = []

        # Add all STUN URLs
        stun_list = [u.strip() for u in self.STUN_URLS.split(",") if u.strip()]
        if stun_list:
            servers.append({"urls": stun_list})

        # Add TURN server if configured
        if self.TURN_URL and self.TURN_USERNAME and self.TURN_CREDENTIAL:
            servers.append({
                "urls":       self.TURN_URL,
                "username":   self.TURN_USERNAME,
                "credential": self.TURN_CREDENTIAL,
            })

        return servers


# Singleton instance used across the app
cfg = Config()


"""
Usage:
    from config import cfg

    cfg.SECRET_KEY
    cfg.ice_servers()   # returns the list to send to the browser
─────────────────────────────────────────────────────────────────────────────
"""