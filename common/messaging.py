"""Kafka producer/consumer wrappers used by the dispatcher and worker.

Thin abstractions over confluent-kafka so the component logic can be tested
with in-memory doubles. The producer publishes the JSON envelope keyed by
job id (keeping a job's messages on one partition); the consumer commits
offsets manually so the worker can commit *after* its DB write lands.
"""

from __future__ import annotations

import json
from typing import Protocol


class Producer(Protocol):
    def publish(self, topic: str, key: str, message: dict) -> None: ...
    def close(self) -> None: ...


class Consumer(Protocol):
    def poll(self, timeout: float) -> dict | None: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...


class KafkaProducer:
    def __init__(self, bootstrap_servers: str) -> None:
        from confluent_kafka import Producer as ConfluentProducer

        self._producer = ConfluentProducer({"bootstrap.servers": bootstrap_servers})

    def publish(self, topic: str, key: str, message: dict) -> None:
        self._producer.produce(
            topic,
            key=key.encode("utf-8"),
            value=json.dumps(message).encode("utf-8"),
        )
        self._producer.flush()

    def close(self) -> None:
        self._producer.flush()


class KafkaConsumer:
    def __init__(self, bootstrap_servers: str, group_id: str, topic: str) -> None:
        from confluent_kafka import Consumer as ConfluentConsumer

        self._consumer = ConfluentConsumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
            }
        )
        self._consumer.subscribe([topic])

    def poll(self, timeout: float) -> dict | None:
        msg = self._consumer.poll(timeout)
        if msg is None or msg.error():
            return None
        return json.loads(msg.value().decode("utf-8"))

    def commit(self) -> None:
        self._consumer.commit(asynchronous=False)

    def close(self) -> None:
        self._consumer.close()


class InMemoryProducer:
    """Captures published messages in-process. Used for tests."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict]] = []

    def publish(self, topic: str, key: str, message: dict) -> None:
        self.published.append((topic, key, message))

    def close(self) -> None:
        pass
