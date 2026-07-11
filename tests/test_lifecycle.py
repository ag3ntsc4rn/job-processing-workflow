"""End-to-end lifecycle over the in-memory doubles (no services needed)."""

from datetime import timedelta

from common.messaging import InMemoryProducer
from common.models import JobStatus, utcnow
from common.store import InMemoryStore
from dispatcher.service import dispatch_once
from handler.service import enqueue
from reaper.service import reap_once
from worker.service import process


def _ok(_payload):
    return None


def _drain_to_worker(store, producer, lookup):
    """Dispatch everything queued, then process each published message."""
    dispatch_once(store, producer, "jobs", 100)
    outcomes = []
    for _topic, _key, envelope in producer.published:
        outcomes.append(process(store, lookup, envelope))
    return outcomes


def test_happy_path_end_to_end():
    store = InMemoryStore()
    producer = InMemoryProducer()

    job_id = enqueue(store, "hello", {"name": "Ada"}).job_id
    outcomes = _drain_to_worker(store, producer, lambda _t: _ok)

    assert outcomes == ["completed"]
    assert store.get_job(job_id).status == JobStatus.COMPLETED


def test_stuck_run_recovers_and_completes_on_redispatch():
    store = InMemoryStore()
    store.set_type_config("hello", run_timeout=timedelta(seconds=1), max_attempts=3)
    producer = InMemoryProducer()

    job_id = enqueue(store, "hello").job_id
    # dispatch + claim, then the worker "dies" before completing
    dispatch_once(store, producer, "jobs", 100)
    store.claim(job_id)
    store._jobs[job_id].updated_at = utcnow() - timedelta(seconds=10)

    # reaper re-queues and re-arms the outbox
    assert reap_once(store).requeued == 1
    assert store.get_job(job_id).status == JobStatus.QUEUED

    # a fresh dispatch + a healthy worker finishes it
    producer2 = InMemoryProducer()
    outcomes = _drain_to_worker(store, producer2, lambda _t: _ok)
    assert outcomes == ["completed"]
    assert store.get_job(job_id).status == JobStatus.COMPLETED


def test_second_trigger_while_active_is_deduped():
    store = InMemoryStore()
    enqueue(store, "hello")
    assert enqueue(store, "hello").enqueued is False
