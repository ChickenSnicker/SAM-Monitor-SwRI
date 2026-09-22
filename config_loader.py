"""
config_loader.py — Loads config.yaml, applies environment-variable overrides,
and validates the result.

Environment overrides exist so that secrets never have to be committed to a
repository. In GitHub Actions these are supplied from encrypted repo secrets.
Anything set in the environment wins over the YAML file.

Supported variables
-------------------
  SAM_API_KEY            sam.api_key
  SAM_LOOKBACK_DAYS      sam.lookback_days
  SAM_KEYWORDS           filters.keywords         (comma-separated)
  SAM_NAICS_CODES        filters.naics_codes      (comma-separated)
  SAM_STATE              filters.state

  SLACK_ENABLED          slack.enabled            (true/false)
  SLACK_WEBHOOK_URL      slack.webhook_url
  SLACK_CHANNEL          slack.channel

  EMAIL_ENABLED          email.enabled            (true/false)
  EMAIL_SMTP_HOST        email.smtp_host
  EMAIL_SMTP_PORT        email.smtp_port
  EMAIL_USERNAME         email.username
  EMAIL_PASSWORD         email.password
  EMAIL_FROM             email.from_address
  EMAIL_TO               email.to_addresses       (comma-separated)

  STORAGE_BACKEND        storage.backend          (json/sqlite)
  LOG_LEVEL              logging.level
"""
import logging
import os
import sys
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

PLACEHOLDERS = {
    "YOUR_SAM_GOV_API_KEY",
    "YOUR_APP_PASSWORD",
    "",
}


# ─────────────────────────────────────────────
#  Coercion helpers
# ─────────────────────────────────────────────

def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_list(raw: str) -> list:
    """Split a comma-separated env var into a clean list."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _as_int(raw: str, fallback: Optional[int] = None) -> Optional[int]:
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return fallback


def _set_nested(cfg: dict, section: str, key: str, value: Any) -> None:
    cfg.setdefault(section, {})[key] = value


# ─────────────────────────────────────────────
#  Loading
# ─────────────────────────────────────────────

def load_config(path: str = "config.yaml", validate: bool = True) -> dict:
    """Load configuration from YAML, apply env overrides, then validate."""
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, path)

    if not os.path.exists(path):
        logger.error("Config file not found: %s", path)
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    applied = _apply_env_overrides(cfg)
    if applied:
        logger.info("Applied %d setting(s) from environment variables.", applied)

    if validate:
        _validate(cfg)
    return cfg


def _apply_env_overrides(cfg: dict) -> int:
    """Overlay environment variables onto the config. Returns count applied."""
    # (env var, section, key, coercion function or None for raw string)
    simple_map = [
        ("SAM_API_KEY",       "sam",     "api_key",      None),
        ("SAM_STATE",         "filters", "state",        None),
        ("SLACK_WEBHOOK_URL", "slack",   "webhook_url",  None),
        ("SLACK_CHANNEL",     "slack",   "channel",      None),
        ("EMAIL_SMTP_HOST",   "email",   "smtp_host",    None),
        ("EMAIL_USERNAME",    "email",   "username",     None),
        ("EMAIL_PASSWORD",    "email",   "password",     None),
        ("EMAIL_FROM",        "email",   "from_address", None),
        ("STORAGE_BACKEND",   "storage", "backend",      None),
        ("LOG_LEVEL",         "logging", "level",        None),

        ("SLACK_ENABLED",     "slack",   "enabled",      _as_bool),
        ("EMAIL_ENABLED",     "email",   "enabled",      _as_bool),

        ("SAM_KEYWORDS",      "filters", "keywords",     _as_list),
        ("SAM_NAICS_CODES",   "filters", "naics_codes",  _as_list),
        ("EMAIL_TO",          "email",   "to_addresses", _as_list),

        ("SAM_LOOKBACK_DAYS", "sam",     "lookback_days", _as_int),
        ("EMAIL_SMTP_PORT",   "email",   "smtp_port",     _as_int),
    ]

    applied = 0
    for env_var, section, key, coerce in simple_map:
        raw = os.environ.get(env_var)
        if raw is None or raw.strip() == "":
            continue
        value = coerce(raw) if coerce else raw.strip()
        if value is None or value == []:
            continue
        _set_nested(cfg, section, key, value)
        applied += 1

    return applied


# ─────────────────────────────────────────────
#  Validation
# ─────────────────────────────────────────────

def _validate(cfg: dict) -> None:
    """Fail fast with actionable messages if the config is unusable."""
    errors = []

    api_key = str(cfg.get("sam", {}).get("api_key", "") or "").strip()
    if api_key in PLACEHOLDERS:
        errors.append(
            "sam.api_key is not set. Add it to config.yaml, or set the "
            "SAM_API_KEY environment variable / GitHub secret. "
            "Get a free key at https://sam.gov (Account Details)."
        )

    filters = cfg.get("filters", {})
    if not filters.get("keywords") and not filters.get("naics_codes"):
        errors.append("filters: at least one keyword or NAICS code is required.")

    backend = str(cfg.get("storage", {}).get("backend", "json")).lower()
    if backend not in {"json", "sqlite"}:
        errors.append(f"storage.backend must be 'json' or 'sqlite' (got '{backend}').")

    slack = cfg.get("slack", {})
    email = cfg.get("email", {})

    if slack.get("enabled"):
        webhook = str(slack.get("webhook_url", "") or "")
        if not webhook or "YOUR/WEBHOOK" in webhook:
            errors.append(
                "slack.enabled is true but slack.webhook_url is not configured "
                "(set SLACK_WEBHOOK_URL or edit config.yaml)."
            )
        elif not webhook.startswith("https://hooks.slack.com/"):
            errors.append("slack.webhook_url does not look like a Slack webhook URL.")

    if email.get("enabled"):
        if not email.get("username"):
            errors.append("email.enabled is true but email.username is not set.")
        if str(email.get("password", "")) in PLACEHOLDERS:
            errors.append("email.enabled is true but email.password is not set.")
        if not email.get("to_addresses"):
            errors.append("email.enabled is true but email.to_addresses is empty.")

    # A run with no delivery channel would silently do nothing useful.
    if not slack.get("enabled") and not email.get("enabled"):
        logger.warning(
            "Neither Slack nor email is enabled — new matches will only be logged. "
            "Enable at least one channel to receive alerts."
        )

    if errors:
        logger.error("Configuration is invalid:")
        for e in errors:
            logger.error("  • %s", e)
        sys.exit(1)
