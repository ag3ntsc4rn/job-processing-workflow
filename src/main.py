"""Entrypoint for running the API with uvicorn.

Because this project uses a ``src/`` layout, ``src`` must be on the import path.
Any of these work:

    uvicorn app:app --app-dir src          # from the repo root
    (cd src && uvicorn app:app)            # from src/
    PYTHONPATH=src python -m main          # honours HOST/PORT from the env

TLS is terminated at the API gateway (Apigee), so this serves plain HTTP and
trusts the proxy for forwarded client info (``proxy_headers``).
"""

from __future__ import annotations

import uvicorn

from app import app
from config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
