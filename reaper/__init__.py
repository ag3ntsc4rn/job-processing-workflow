"""Reaper: recovers jobs stuck in ``running``.

A worker that dies mid-run leaves the job ``running``; since that's an active
status, the dedup index would block all future runs of that type. The reaper
sweeps on a timer: past a per-type timeout it re-queues the job (and re-arms
the outbox) so the dispatcher publishes a fresh message, or dead-letters it to
``failed`` once the churn cap is hit. Runs as a single replica.
"""
