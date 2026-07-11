from datetime import timedelta

from common.messaging import InMemoryProducer
from common.models import JobStatus, utcnow
from common.store import InMemoryStore
from dispatcher.service import dispatch_once
from handler.service import enqueue
from reaper.service import reap_once


def _make_running(store, job_type, max_attempts=3):
    store.set_type_config(job_type, run_timeout=timedelta(seconds=1), max_attempts=max_attempts)
    job_id = enqueue(store, job_type).job_id
    dispatch_once(store, InMemoryProducer(), "jobs", 10)
    store.claim(job_id)
    return job_id


def _age(store, job_id, seconds):
    store._jobs[job_id].updated_at = utcnow() - timedelta(seconds=seconds)


def test_fresh_running_job_is_not_reaped():
    store = InMemoryStore()
    _make_running(store, "hello")
    assert reap_once(store).requeued == 0


def test_stuck_job_is_requeued_and_outbox_rearmed():
    store = InMemoryStore()
    job_id = _make_running(store, "hello")
    _age(store, job_id, 10)

    stats = reap_once(store)

    assert stats.requeued == 1
    job = store.get_job(job_id)
    assert job.status == JobStatus.QUEUED
    assert job.attempts == 1
    # re-armed: the dispatcher can publish it again
    assert any(m.job_id == job_id for m in store.fetch_unpublished(10))


def test_churn_cap_dead_letters_after_max_attempts():
    store = InMemoryStore()
    job_id = _make_running(store, "hello", max_attempts=1)
    # bump attempts to the cap, then get it stuck in running again
    store._jobs[job_id].attempts = 1
    _age(store, job_id, 10)

    stats = reap_once(store)

    assert stats.requeued == 0
    assert stats.dead_lettered == 1
    assert store.get_job(job_id).status == JobStatus.FAILED
