"""HTTP API in front of the job-enqueue contract.

A small FastAPI service (the ``handler`` folder's long-running sibling) that
lets AutoSys, other services, and humans enqueue and look up jobs over HTTP,
secured as an OAuth2/OIDC resource server (Ping Federate). It reuses the shared
``common.store`` enqueue transaction, so the jobs+outbox+dedup invariant still
lives in exactly one place.
"""
