"""handlerAPIv2 — the HTTP front door that sits behind an Apigee proxy.

Same contract as ``handlerAPI`` (``POST /v1/jobs``, ``GET /v1/jobs/{id}``, health
probes, same ``common.Store`` enqueue transaction and Postgres schema) but with a
different trust model: an API gateway (Apigee) terminates the edge (quota, spike
arrest, threat protection, TLS) and hands the service a JWT carrying claims +
scopes. The service is a plain JWT resource server — *who signs the token is pure
config* (``OIDC_ISSUER`` / ``OIDC_JWKS_URL`` / ``OIDC_AUDIENCE``): Keycloak
directly in local dev (no gateway in the loop), Apigee's minted token in prod.
"""
