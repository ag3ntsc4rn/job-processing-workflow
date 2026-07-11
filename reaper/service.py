"""Reaper logic — pure, so it unit-tests against any ``Store``."""

from __future__ import annotations

from dataclasses import dataclass

from common.store import Store


@dataclass
class ReapStats:
    requeued: int = 0
    dead_lettered: int = 0


def reap_once(store: Store) -> ReapStats:
    """Recover every stuck run found this sweep.

    Under the churn cap: re-queue + re-arm the outbox. At the cap:
    ``requeue_stuck`` returns False and we dead-letter to ``failed`` instead,
    which also frees the dedup slot for the next schedule.
    """
    stats = ReapStats()
    for stuck in store.find_stuck():
        if store.requeue_stuck(stuck.job_id):
            stats.requeued += 1
        elif store.dead_letter(stuck.job_id):
            stats.dead_lettered += 1
    return stats
