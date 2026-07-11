"""Job-type -> handler registry.

A handler is any callable taking the job payload. Register with the decorator:

    @register("send_report")
    def send_report(payload: dict) -> None:
        ...

Raising from a handler marks the run ``failed`` (Option A: the next AutoSys
schedule re-enqueues it). This module is the one place ``job_type`` maps to
behaviour.
"""

from __future__ import annotations

from collections.abc import Callable

Handler = Callable[[dict], None]

_REGISTRY: dict[str, Handler] = {}


def register(job_type: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        if job_type in _REGISTRY:
            raise ValueError(f"handler for {job_type!r} already registered")
        _REGISTRY[job_type] = fn
        return fn

    return decorator


def get_handler(job_type: str) -> Handler | None:
    return _REGISTRY.get(job_type)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)
