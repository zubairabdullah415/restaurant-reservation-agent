"""
config.py — Application Configuration
=======================================
All settings are loaded from environment variables.
Copy .env.example to .env and fill in your credentials.
"""

from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Core ────────────────────────────────────────────────────────────────
    DEBUG: bool = False
    SECRET_KEY: str                          # Used for any server-side signing

    # ── Database ────────────────────────────────────────────────────────────
    DATABASE_URL: str                        # postgresql://user:pass@host:5432/dbname

    # ── Anthropic (Claude) ──────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str

    # ── SendGrid ────────────────────────────────────────────────────────────
    SENDGRID_API_KEY: str
    EMAIL_FROM_ADDRESS: str = "reservations@thegrandolive.com"
    EMAIL_FROM_NAME: str    = "The Grand Olive"

    # ── Twilio ──────────────────────────────────────────────────────────────
    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN:  str
    TWILIO_FROM_NUMBER: str                  # E.164 format: +447911000000

    # ── CORS / Security ─────────────────────────────────────────────────────
    ALLOWED_ORIGINS: List[str] = ["https://www.thegrandolive.com", "http://localhost:3000"]
    ALLOWED_HOSTS:   List[str] = ["api.thegrandolive.com", "localhost"]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
