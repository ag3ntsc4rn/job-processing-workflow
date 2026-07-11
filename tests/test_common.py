from common.config import Config
from common.models import JobStatus, build_envelope


def test_envelope_shape():
    # Pure pointer: no business payload rides on the wire.
    assert build_envelope(7, "hello") == {"job_id": 7, "job_type": "hello"}


def test_active_and_terminal_states_are_disjoint():
    assert set(JobStatus.ACTIVE).isdisjoint(JobStatus.TERMINAL)
    assert JobStatus.QUEUED in JobStatus.ACTIVE
    assert JobStatus.COMPLETED in JobStatus.TERMINAL


def test_config_defaults(monkeypatch):
    for var in (
        "DATABASE_URL",
        "KAFKA_BOOTSTRAP_SERVERS",
        "KAFKA_TOPIC",
        "CONSUMER_GROUP",
        "DISPATCHER_BATCH_SIZE",
        "DISPATCHER_POLL_INTERVAL",
        "REAPER_POLL_INTERVAL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = Config.from_env()
    assert cfg.kafka_topic == "jobs"
    assert cfg.consumer_group == "workers"
    assert cfg.dispatcher_batch_size == 100
    assert cfg.reaper_poll_interval == 60.0


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("KAFKA_TOPIC", "custom")
    monkeypatch.setenv("DISPATCHER_BATCH_SIZE", "5")
    cfg = Config.from_env()
    assert cfg.kafka_topic == "custom"
    assert cfg.dispatcher_batch_size == 5
