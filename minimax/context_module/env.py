"""Environment-driven defaults for the SDD context module.

Every connection setting comes from the environment, so the module runs on any
machine without editing code:

    SEEDB_HOST      (fallback SEEKDB_HOST)      default 127.0.0.1
    SEEDB_PORT      (fallback SEEKDB_PORT)      default 2881
    SEEDB_USER      (fallback SEEKDB_USER)      default root
    SEEDB_PASSWORD  (fallback SEEKDB_PASSWORD)  default empty
    SEEDB_DATABASE  (fallback SEEKDB_DATABASE)  default test
"""

from __future__ import annotations

import os

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 2881,
    "user": "root",
    "password": "",
    "database": "test",
}


def _first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return value
    return default


def defaults() -> dict:
    """Return the effective seekdb connection defaults."""
    return {
        "host": _first("SEEDB_HOST", "SEEKDB_HOST", default=DEFAULTS["host"]),
        "port": int(_first("SEEDB_PORT", "SEEKDB_PORT", default=str(DEFAULTS["port"]))),
        "user": _first("SEEDB_USER", "SEEKDB_USER", default=DEFAULTS["user"]),
        "password": _first("SEEDB_PASSWORD", "SEEKDB_PASSWORD", default=DEFAULTS["password"]),
        "database": _first("SEEDB_DATABASE", "SEEKDB_DATABASE", default=DEFAULTS["database"]),
    }