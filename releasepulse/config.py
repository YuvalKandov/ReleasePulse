"""Application settings, loaded from environment variables.

Kept separate from db.py on purpose: this holds API and security configuration
(admin token, run mode, SSRF allowlist). The database connection URL stays in
db.py. One typed source of truth instead of scattered os.environ reads.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Read from real environment variables (case-insensitive) and, if present,
    # a local .env file. Unknown keys are ignored rather than erroring.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Bearer token required on every mutating API call. No default: the app
    # must be told its admin token explicitly.
    admin_token: str

    # Shared secret for the deployment webhook (distinct from the admin token:
    # CI/CD holds this, not admin credentials). From Phase 1 it is the HMAC key:
    # the sender signs "<timestamp>.<raw_body>" with it, the receiver recomputes.
    webhook_secret: str

    # How far a webhook's signed timestamp may drift from now (seconds) before we
    # reject it as a possible replay. 300s tolerates clock skew + delivery latency
    # while keeping the replay window short.
    webhook_hmac_window_sec: int = 300

    # 'production' rejects every non-globally-routable destination.
    # 'dev' additionally permits the hosts/CIDRs listed in ssrf_allowlist,
    # so the future Docker Compose demo (e.g. http://demo-service:8080) works
    # without weakening production behaviour.
    app_env: str = "production"

    # Comma-separated hostnames or CIDRs permitted only when app_env == 'dev'.
    ssrf_allowlist: str = ""

    # Telegram alert channel. Both must be set for the worker to dispatch alerts;
    # if either is missing, alerting is disabled and the worker logs it once. Kept
    # optional so the app runs locally and tests can inject a fake sender instead.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # Detector window sizes the worker's auto-evaluation uses. Defaults are the
    # frozen spec values; the local Compose stack shortens them so the loop can be
    # watched in minutes instead of the full ~13. Production keeps the defaults.
    detector_baseline_sec: int = 600
    detector_warmup_sec: int = 180
    detector_observation_sec: int = 600
    detector_min_samples: int = 15
    detector_min_successful_baseline: int = 10

    # Port the worker's internal HTTP server binds for /healthz, /readyz, /metrics.
    # Distinct from the API's 8000; Kubernetes probes and Prometheus scrape it.
    worker_http_port: int = 8001


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    return Settings()
