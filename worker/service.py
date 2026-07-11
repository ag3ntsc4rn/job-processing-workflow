"""Worker processing logic — pure, so it unit-tests against any ``Store``."""

from __future__ import annotations

from collections.abc import Callable

from common.store import Store

Handler = Callable[[dict], None]
Lookup = Callable[[str], Handler | None]


def process(store: Store, lookup: Lookup, envelope: dict) -> str:
    """Claim, run, and record one job message. Returns the outcome.

    Outcomes: ``"skipped"`` (lost the claim -> duplicate/redelivery),
    ``"completed"``, ``"no_config"`` (type has no ``job_type_config`` row, so
    there's no payload to run it with -> failed gracefully), or ``"failed"``
    (unknown type or handler raised).

    The message envelope is a pure pointer (``job_id``/``job_type``); the
    business payload is snapshotted from ``job_type_config`` at claim time.
    """
    job_id = envelope["job_id"]
    job_type = envelope["job_type"]

    if not store.claim(job_id):
        return "skipped"  # another worker already owns this run

    payload = store.snapshot_config_payload(job_id, job_type)
    if payload is None:
        store.fail(job_id)
        return "no_config"  # no config for this type -> nothing to run

    handler = lookup(job_type)
    if handler is None:
        store.fail(job_id)
        return "failed"

    try:
        handler(payload)
    except Exception:  # noqa: BLE001 - any handler error fails the run (Option A)
        store.fail(job_id)
        return "failed"

    store.complete(job_id)
    return "completed"
