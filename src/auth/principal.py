"""The authenticated caller, distilled from validated JWT claims.

The JWT is validated upstream by the enterprise auth middleware; this module only
*reads* the resulting claims. v2 is machine-to-machine only (customers
authenticate with client-id/secret via ``client_credentials``), so every
principal is a ``service`` — there is no human/user branch and no per-user
ownership. The ``created_by`` audit trail on the job still records the identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.models import Creator


@dataclass(frozen=True)
class Principal:
    subject: str
    client_id: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def to_creator(self) -> Creator:
        return Creator(
            sub=self.subject,
            type="service",
            client_id=self.client_id,
        )

    @classmethod
    def from_claims(cls, claims: dict) -> Principal:
        """Build a principal from a set of already-validated JWT claims."""
        client_id = claims.get("client_id") or claims.get("azp")
        subject = claims.get("sub") or client_id or "unknown"
        return cls(
            subject=str(subject),
            client_id=client_id,
            scopes=extract_scopes(claims),
        )


def extract_scopes(claims: dict) -> frozenset[str]:
    # OAuth2 access tokens carry scopes either as a space-delimited "scope"
    # string (RFC standard, Keycloak's default) or a "scp" list (some IdPs).
    scope = claims.get("scope")
    if isinstance(scope, str):
        return frozenset(scope.split())
    scp = claims.get("scp")
    if isinstance(scp, list):
        return frozenset(str(s) for s in scp)
    return frozenset()
