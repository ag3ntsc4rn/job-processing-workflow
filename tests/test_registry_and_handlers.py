import pytest

import worker.handlers  # noqa: F401  (registers demo handlers)
from worker.handlers.demo import boom, hello
from worker.registry import get_handler, register, registered_types


def test_demo_handlers_are_registered():
    types = registered_types()
    assert "hello" in types
    assert "boom" in types


def test_get_handler_returns_none_for_unknown():
    assert get_handler("does-not-exist") is None


def test_duplicate_registration_raises():
    with pytest.raises(ValueError):

        @register("hello")
        def _dupe(_payload):
            return None


def test_hello_handler_runs(capsys):
    hello({"name": "Ada"})
    assert "Ada" in capsys.readouterr().out


def test_boom_handler_raises():
    with pytest.raises(RuntimeError):
        boom({})
