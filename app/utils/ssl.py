from __future__ import annotations

import os
import ssl
from pathlib import Path

import certifi


def build_ssl_context() -> ssl.SSLContext:
    """
    Универсальный SSL context:
    - по умолчанию доверяет certifi CA bundle (кроссплатформенно)
    - при наличии CUSTOM_CA_BUNDLE позволяет подмешать/заменить CA
    """
    ctx = ssl.create_default_context(cafile=certifi.where())

    custom = os.getenv("CUSTOM_CA_BUNDLE", "").strip()
    if custom:
        p = Path(custom)
        if p.exists():
            ctx.load_verify_locations(cafile=str(p))
    return ctx
