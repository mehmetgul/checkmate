"""Encryption utilities for sensitive data."""

import os
from cryptography.fernet import Fernet

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

# Lazy initialization - only validate when actually used
_fernet = None


def _get_fernet():
    """Get or create Fernet instance."""
    global _fernet
    if _fernet is None:
        if not ENCRYPTION_KEY:
            raise ValueError("ENCRYPTION_KEY environment variable must be set")
        _fernet = Fernet(ENCRYPTION_KEY.encode())
    return _fernet


def encrypt_password(plain: str) -> str:
    """Encrypt a plain text password."""
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    """Decrypt an encrypted password."""
    return _get_fernet().decrypt(encrypted.encode()).decode()
