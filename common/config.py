"""Runtime configuration, read from the environment.

Every component reads the same ``Config``; each uses the subset it needs. The
docker-compose stack sets these for you; locally they have sensible defaults so
you can point at the compose Postgres/Kafka with no extra flags.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    database_url: str
    kafka_bootstrap_servers: str
    kafka_topic: str
    consumer_group: str
    dispatcher_batch_size: int
    dispatcher_poll_interval: float
    reaper_poll_interval: float

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            database_url=os.getenv(
                "DATABASE_URL", "postgresql://app:app@localhost:5432/app"
            ),
            kafka_bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            kafka_topic=os.getenv("KAFKA_TOPIC", "jobs"),
            consumer_group=os.getenv("CONSUMER_GROUP", "workers"),
            dispatcher_batch_size=int(os.getenv("DISPATCHER_BATCH_SIZE", "100")),
            dispatcher_poll_interval=float(os.getenv("DISPATCHER_POLL_INTERVAL", "1.0")),
            reaper_poll_interval=float(os.getenv("REAPER_POLL_INTERVAL", "60.0")),
        )
