from common.models import JobStatus
from common.store import InMemoryStore
from handler.service import enqueue


def test_enqueue_creates_queued_job_and_outbox():
    store = InMemoryStore()
    result = enqueue(store, "hello", {"name": "Ada"})
    assert result.enqueued is True
    job = store.get_job(result.job_id)
    assert job.status == JobStatus.QUEUED
    assert job.job_type == "hello"
    # an outbox message was written for the job
    assert store.fetch_unpublished(10)[0].job_id == result.job_id


def test_enqueue_dedups_active_job_of_same_type():
    store = InMemoryStore()
    first = enqueue(store, "hello")
    second = enqueue(store, "hello")
    assert first.enqueued is True
    assert second.enqueued is False
    assert second.job_id is None


def test_enqueue_allows_new_run_after_terminal():
    store = InMemoryStore()
    first = enqueue(store, "hello")
    # drive first to a terminal state
    msg = store.fetch_unpublished(1)[0]
    store.mark_dispatched(msg.id, first.job_id)
    store.claim(first.job_id)
    store.complete(first.job_id)
    # now a new active job of the same type is allowed
    second = enqueue(store, "hello")
    assert second.enqueued is True
    assert second.job_id != first.job_id


def test_enqueue_defaults_payload_to_empty_dict():
    store = InMemoryStore()
    result = enqueue(store, "hello")
    assert store.get_job(result.job_id).payload == {}
