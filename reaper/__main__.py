"""Reaper service loop: ``python -m reaper``."""

from __future__ import annotations

import sys
import time

from common.config import Config
from common.db import PostgresStore
from reaper.service import reap_once


def main() -> int:
    cfg = Config.from_env()
    store = PostgresStore(cfg.database_url)
    sys.stdout.write("reaper started\n")
    try:
        while True:
            stats = reap_once(store)
            if stats.requeued or stats.dead_lettered:
                sys.stdout.write(
                    f"reaped: requeued={stats.requeued} dead_lettered={stats.dead_lettered}\n"
                )
            time.sleep(cfg.reaper_poll_interval)
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
