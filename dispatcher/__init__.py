"""Dispatcher: drains the outbox to Kafka.

Polls unpublished outbox rows (``FOR UPDATE SKIP LOCKED``, so multiple
dispatchers are safe), publishes each to Kafka keyed by job id, then marks the
row published and the job ``dispatched``. Type-agnostic: it copies bytes.
"""
