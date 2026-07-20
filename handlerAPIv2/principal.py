"""The authenticated caller, distilled from a validated JWT.

v2 is machine-to-machine only (customers authenticate with client-id/secret via
``client_credentials``), so every principal is a ``service`` — there is no
human/user branch and no per-user ownership. The ``created_by`` audit trail on
the job still records the calling identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from common.models import Creator


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
