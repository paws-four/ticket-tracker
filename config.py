"""Configuration and secrets loading.

All secrets live in a gitignored .env file (see .env.example). Nothing here
raises on missing values — the app must boot and show a clear "not configured"
state in /settings instead of crashing.
"""
import os

from dotenv import load_dotenv

# Load .env from the project directory (next to this file).
_BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

BASE_DIR = _BASE_DIR
DB_PATH = os.path.join(_BASE_DIR, "tracker.db")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")

# HTTP fetching
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 WebsiteTracker/1.0",
)
FETCH_TIMEOUT = int(os.getenv("FETCH_TIMEOUT", "20"))
# If the plain-HTTP extracted text is shorter than this, we suspect a
# JavaScript-rendered page and fall back to a headless browser.
MIN_TEXT_LENGTH = int(os.getenv("MIN_TEXT_LENGTH", "500"))

DEFAULT_CHECK_INTERVAL_MINUTES = int(os.getenv("DEFAULT_CHECK_INTERVAL_MINUTES", "15"))
# Fraction of jitter applied to each interval (0.2 == +/-20%).
SCHEDULE_JITTER = float(os.getenv("SCHEDULE_JITTER", "0.2"))
# Consecutive errors after which an item is auto-paused to stop hammering a site.
MAX_CONSECUTIVE_ERRORS = int(os.getenv("MAX_CONSECUTIVE_ERRORS", "6"))

DEFAULT_AVAILABLE_PHRASES = [
    "Register Now",
    "Buy Tickets",
    "Buy Now",
    "Sign Up",
    "Get Tickets",
    "Enter Now",
    "Add to Cart",
    "On Sale",
    "Register",
]
DEFAULT_UNAVAILABLE_PHRASES = [
    "Sold Out",
    "Coming Soon",
    "Notify Me",
    "Registration Closed",
    "Closed",
    "Waitlist",
    "Join the Waitlist",
    "Not Yet Available",
    "Currently Unavailable",
]


def _clean_list(raw):
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


class Notifications:
    """Snapshot of notification config read from the environment."""

    SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
    SMTP_USER = os.getenv("SMTP_USER", "").strip()
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
    NOTIFY_EMAIL_TO = _clean_list(os.getenv("NOTIFY_EMAIL_TO", ""))

    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    NOTIFY_SMS_TO = _clean_list(os.getenv("NOTIFY_SMS_TO", ""))

    # --- ntfy push (free) ---
    NTFY_SERVER = (os.getenv("NTFY_SERVER", "https://ntfy.sh").strip() or "https://ntfy.sh")
    NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
    # Optional access token for protected/self-hosted topics.
    NTFY_TOKEN = os.getenv("NTFY_TOKEN", "").strip()

    @classmethod
    def email_configured(cls):
        return bool(
            cls.SMTP_HOST and cls.SMTP_USER and cls.SMTP_PASSWORD and cls.NOTIFY_EMAIL_TO
        )

    @classmethod
    def sms_configured(cls):
        return bool(
            cls.TWILIO_ACCOUNT_SID
            and cls.TWILIO_AUTH_TOKEN
            and cls.TWILIO_FROM_NUMBER
            and cls.NOTIFY_SMS_TO
        )

    @classmethod
    def ntfy_configured(cls):
        return bool(cls.NTFY_TOPIC)

    @classmethod
    def status(cls):
        """Human-friendly config status for the settings page (no secrets leaked)."""
        return {
            "email_configured": cls.email_configured(),
            "sms_configured": cls.sms_configured(),
            "ntfy_configured": cls.ntfy_configured(),
            "smtp_host": cls.SMTP_HOST or None,
            "smtp_port": cls.SMTP_PORT,
            "smtp_user": cls.SMTP_USER or None,
            "email_to": cls.NOTIFY_EMAIL_TO,
            "twilio_sid_present": bool(cls.TWILIO_ACCOUNT_SID),
            "twilio_from": cls.TWILIO_FROM_NUMBER or None,
            "sms_to": cls.NOTIFY_SMS_TO,
            "ntfy_server": cls.NTFY_SERVER,
            "ntfy_topic": cls.NTFY_TOPIC or None,
        }
