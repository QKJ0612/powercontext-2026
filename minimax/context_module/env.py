"""Resolve seekdb connection settings from the environment.

The competition / review machine is expected to run a stock seekdb on the default
port 2881.  Local experiments here used 2896, so every script reads the same
environment variables instead of hard-coding a port:

    SEEDB_HOST      default 127.0.0.1
    SEEDB_PORT      default 2881
    SEEDB_USER      default root
    SEEDB_PASSWORD  default empty
    SEEDB_DATABASE  default test

The older ``SEEKDB_*`` spelling is accepted as a fallback so existing shells and
the previous run_demo.sh keep working.
"""

from __future__ import annotations

import os


def setting(name: str, default: str) -> str:
    return os.environ.get(f"SEEDB_{name}") or os.environ.get(f"SEEKDB_{name}") or default


def defaults() -> dict[str, object]:
    return {
        "host": setting("HOST", "127.0.0.1"),
        "port": int(setting("PORT", "2881")),
        "user": setting("USER", "root"),
        "password": setting("PASSWORD", ""),
        "database": setting("DATABASE", "test"),
    }
