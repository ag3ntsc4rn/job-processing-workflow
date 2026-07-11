"""Worker processing logic — pure, so it unit-tests against any ``Store``."""

from __future__ import annotations

from collections.abc import Callable

from common.store import Store

Handler = Callable[[dict], None]
Lookup = Callable[[str], Handler | None]


def process(store: Store, lookup: Lookup, envelope: dict) -> str:
    """Claim, run, and record one job message. Returns the outcome.

    Outcomes: ``"skipped"`` (lost the claim -> duplicate/redelivery),
    ``"completed"``, or ``"failed"`` (unknown type or handler raised).
    """
    job_id = envelope["job_id"]
    job_type = envelope["job_type"]
    payload = envelope.get("payload", {})

    if not store.claim(job_id):
        return "skipped"  # another worker already owns this run

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
