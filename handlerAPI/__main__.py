"""Run the API with uvicorn: ``python -m handlerAPI``.

TLS is terminated in-app when ``TLS_CERTFILE`` / ``TLS_KEYFILE`` are set (the
interim setup); once an API gateway / load balancer fronts the service, drop
those and let it terminate TLS, adding ``--proxy-headers`` so client IPs survive.
"""

from __future__ import annotations

import uvicorn

from handlerAPI.config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "handlerAPI.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        ssl_certfile=settings.tls_certfile,
        ssl_keyfile=settings.tls_keyfile,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
