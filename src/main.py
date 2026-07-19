"""Run the API with uvicorn: ``python -m main`` (or ``python src/main.py``).

TLS is terminated at the API gateway (Apigee), so this serves plain HTTP and
trusts the proxy for forwarded client info (``--proxy-headers``).
"""

from __future__ import annotations

import uvicorn

from config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
