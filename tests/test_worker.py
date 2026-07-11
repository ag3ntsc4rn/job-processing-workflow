from common.messaging import InMemoryProducer
from common.models import JobStatus, build_envelope
from common.store import InMemoryStore
from dispatcher.service import dispatch_once
from handler.service import enqueue
from worker.service import process


def _dispatch(store, job_id):
    dispatch_once(store, InMemoryProducer(), "jobs", 10)
    return build_envelope(job_id, store.get_job(job_id).job_type)


def _ok(_payload):
    return None


def _raise(_payload):
    raise RuntimeError("nope")


def test_process_completes_job():
    store = InMemoryStore()
    store.set_type_config("hello", payload={"n": 1})
    job_id = enqueue(store, "hello").job_id
    env = _dispatch(store, job_id)

    outcome = process(store, lambda _t: _ok, env)

    assert outcome == "completed"
    assert store.get_job(job_id).status == JobStatus.COMPLETED


def test_worker_snapshots_config_payload_into_the_run():
    store = InMemoryStore()
    store.set_type_config("hello", payload={"name": "Ada"})
    job_id = enqueue(store, "hello").job_id
    env = _dispatch(store, job_id)

    seen = {}
    process(store, lambda _t: lambda p: seen.update(p), env)

    # the handler received the config payload, and it was snapshotted onto the run
    assert seen == {"name": "Ada"}
    assert store.get_job(job_id).payload == {"name": "Ada"}


def test_process_fails_gracefully_when_type_has_no_config():
    store = InMemoryStore()
    job_id = enqueue(store, "orphan").job_id  # no set_type_config -> no config row
    env = _dispatch(store, job_id)

    outcome = process(store, lambda _t: _ok, env)

    assert outcome == "no_config"
    assert store.get_job(job_id).status == JobStatus.FAILED


def test_process_fails_when_handler_raises():
    store = InMemoryStore()
    store.set_type_config("boom", payload={})
    job_id = enqueue(store, "boom").job_id
    env = _dispatch(store, job_id)

    outcome = process(store, lambda _t: _raise, env)

    assert outcome == "failed"
    assert store.get_job(job_id).status == JobStatus.FAILED


def test_process_fails_on_unknown_type():
    store = InMemoryStore()
    store.set_type_config("mystery", payload={})
    job_id = enqueue(store, "mystery").job_id
    env = _dispatch(store, job_id)

    outcome = process(store, lambda _t: None, env)

    assert outcome == "failed"
    assert store.get_job(job_id).status == JobStatus.FAILED


def test_claim_tolerates_queued_when_worker_outraces_dispatcher():
    store = InMemoryStore()
    store.set_type_config("hello", payload={})
    job_id = enqueue(store, "hello").job_id
    # message delivered before the dispatcher marked the job 'dispatched'
    env = build_envelope(job_id, "hello")
    outcome = process(store, lambda _t: _ok, env)
    assert outcome == "completed"
    assert store.get_job(job_id).status == JobStatus.COMPLETED


def test_duplicate_delivery_is_skipped():
    store = InMemoryStore()
    store.set_type_config("hello", payload={})
    job_id = enqueue(store, "hello").job_id
    env = _dispatch(store, job_id)

    first = process(store, lambda _t: _ok, env)
    second = process(store, lambda _t: _ok, env)  # redelivery of same message

    assert first == "completed"
    assert second == "skipped"
    assert store.get_job(job_id).status == JobStatus.COMPLETED
