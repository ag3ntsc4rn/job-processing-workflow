"""Request/response models. Strict: unknown fields on input are rejected."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_JOB_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: str = Field(..., description="Registered job type to enqueue")
    payload: dict[str, Any] | None = Field(
        default=None,
        description="Optional per-run overrides; merged over the type's base config (input wins)",
    )

    @field_validator("job_type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if not _JOB_TYPE_RE.match(v):
            raise ValueError(
                "job_type must be lowercase alnum with _.- (1-64 chars, leading letter)"
            )
        return v


class CreateJobResponse(BaseModel):
    job_id: int
    status: str


class CreatedBy(BaseModel):
    sub: str | None = None
    type: str | None = None
    client_id: str | None = None


class JobResponse(BaseModel):
    id: int
    job_type: str
    status: str
    input_payload: dict[str, Any]
    payload: dict[str, Any]
    attempts: int
    created_at: datetime
    updated_at: datetime
    created_by: CreatedBy
