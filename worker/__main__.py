"""Worker service loop: ``python -m worker``.

Consumes the jobs topic, processes each message, and commits the Kafka offset
*after* the DB write lands (so a crash in between just redelivers and the claim
guard no-ops the duplicate).
"""

from __future__ import annotations

import sys

import worker.handlers  # noqa: F401  (registers handlers on import)
from common.config import Config
from common.db import PostgresStore
from common.messaging import KafkaConsumer
from worker.registry import get_handler, registered_types
from worker.service import process


def main() -> int:
    cfg = Config.from_env()
    store = PostgresStore(cfg.database_url)
    consumer = KafkaConsumer(cfg.kafka_bootstrap_servers, cfg.consumer_group, cfg.kafka_topic)
    sys.stdout.write(f"worker started; handlers: {registered_types()}\n")
    try:
        while True:
            envelope = consumer.poll(1.0)
            if envelope is None:
                continue
            outcome = process(store, get_handler, envelope)
            consumer.commit()
            job_id = envelope.get("job_id")
            job_type = envelope.get("job_type")
            sys.stdout.write(f"job {job_id} ({job_type}): {outcome}\n")
    except KeyboardInterrupt:
        return 0
    finally:
        consumer.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
