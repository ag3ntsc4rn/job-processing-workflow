"""Dispatch logic — pure, so it unit-tests against any ``Store``/``Producer``."""

from __future__ import annotations

from common.messaging import Producer
from common.store import Store


def dispatch_once(store: Store, producer: Producer, topic: str, batch_size: int) -> int:
    """Publish one batch of unpublished outbox messages. Returns how many.

    Publish happens before ``mark_dispatched``: a crash in between leaves the
    row unpublished so it's retried (at-least-once), and any resulting Kafka
    duplicate is absorbed by the worker's claim guard.
    """
    messages = store.fetch_unpublished(batch_size)
    for msg in messages:
        producer.publish(topic, key=str(msg.job_id), message=msg.payload)
        store.mark_dispatched(msg.id, msg.job_id)
    return len(messages)
