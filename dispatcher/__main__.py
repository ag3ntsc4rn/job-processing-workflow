"""Dispatcher service loop: ``python -m dispatcher``."""

from __future__ import annotations

import sys
import time

from common.config import Config
from common.db import PostgresStore
from common.messaging import KafkaProducer
from dispatcher.service import dispatch_once


def main() -> int:
    cfg = Config.from_env()
    store = PostgresStore(cfg.database_url)
    producer = KafkaProducer(cfg.kafka_bootstrap_servers)
    sys.stdout.write("dispatcher started\n")
    try:
        while True:
            sent = dispatch_once(store, producer, cfg.kafka_topic, cfg.dispatcher_batch_size)
            if sent == 0:
                time.sleep(cfg.dispatcher_poll_interval)
    except KeyboardInterrupt:
        return 0
    finally:
        producer.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
