"""Demo handlers used by the compose stack and e2e walkthrough.

- ``hello``: succeeds, logs a greeting.
- ``boom``: always raises, to demonstrate a failure landing in ``failed``.

Real handlers should be idempotent, since at-least-once delivery + reaper
re-queues mean the work can run more than once.
"""

from __future__ import annotations

import sys

from worker.registry import register


@register("hello")
def hello(payload: dict) -> None:
    name = payload.get("name", "world")
    sys.stdout.write(f"[hello] hello, {name}!\n")


@register("boom")
def boom(payload: dict) -> None:
    raise RuntimeError("boom: simulated business failure")
