"""Application configuration from environment variables."""

import os


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse boolean from environment variable."""
    return os.getenv(name, str(default)).lower() in ("true", "1", "yes")


# Feature flags
INTELLIGENT_RETRY_ENABLED = _env_bool("INTELLIGENT_RETRY_ENABLED", False)
