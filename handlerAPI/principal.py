"""The authenticated caller, distilled from a validated access token."""

from __future__ import annotations

from dataclasses import dataclass, field

from common.models import Creator


@dataclass(frozen=True)
class Principal:
    subject: str
    principal_type: str  # 'user' | 'service'
    client_id: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    groups: tuple[str, ...] = ()

    @property
    def is_service(self) -> bool:
        return self.principal_type == "service"

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def in_group(self, group: str) -> bool:
        return group in self.groups

    def to_creator(self) -> Creator:
        return Creator(
            sub=self.subject,
            type=self.principal_type,
            client_id=self.client_id,
        )
