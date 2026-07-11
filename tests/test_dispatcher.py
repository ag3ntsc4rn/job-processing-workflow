from common.messaging import InMemoryProducer
from common.models import JobStatus
from common.store import InMemoryStore
from dispatcher.service import dispatch_once
from handler.service import enqueue


def test_dispatch_publishes_and_marks_dispatched():
    store = InMemoryStore()
    job_id = enqueue(store, "hello").job_id
    producer = InMemoryProducer()

    sent = dispatch_once(store, producer, "jobs", batch_size=10)

    assert sent == 1
    topic, key, message = producer.published[0]
    assert topic == "jobs"
    assert key == str(job_id)
    assert message == {"job_id": job_id, "job_type": "hello"}
    assert store.get_job(job_id).status == JobStatus.DISPATCHED


def test_dispatch_is_idempotent_across_polls():
    store = InMemoryStore()
    enqueue(store, "hello")
    producer = InMemoryProducer()

    assert dispatch_once(store, producer, "jobs", 10) == 1
    # nothing new to publish on the second poll
    assert dispatch_once(store, producer, "jobs", 10) == 0
    assert len(producer.published) == 1


def test_dispatch_respects_batch_size():
    store = InMemoryStore()
    enqueue(store, "a")
    enqueue(store, "b")
    enqueue(store, "c")
    producer = InMemoryProducer()

    assert dispatch_once(store, producer, "jobs", batch_size=2) == 2
    assert dispatch_once(store, producer, "jobs", batch_size=2) == 1
