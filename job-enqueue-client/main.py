#!/usr/bin/env python3
"""Entrypoint: ``python main.py <job_type> [payload_json]``."""

from __future__ import annotations

from jobclient.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
