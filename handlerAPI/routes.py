"""HTTP routes: enqueue a job, look one up, and health probes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from common.models import Job
from common.store import Store
from handlerAPI.config import Settings
from handlerAPI.deps import (
    can_read_job_created_by,
    get_settings,
    get_store,
    require_read,
    require_write,
)
from handlerAPI.errors import ProblemException
from handlerAPI.principal import Principal
from handlerAPI.schemas import CreatedBy, CreateJobRequest, CreateJobResponse, JobResponse

router = APIRouter()


@router.post(
    "/v1/jobs",
    status_code=201,
    response_model=CreateJobResponse,
    tags=["jobs"],
    summary="Enqueue a job",
)
def create_job(
    body: CreateJobRequest,
    response: Response,
    principal: Principal = Depends(require_write),
    store: Store = Depends(get_store),
) -> CreateJobResponse:
    job_id = store.enqueue(body.job_type, body.payload or {}, creator=principal.to_creator())
    if job_id is None:
        # dedup: an active job of this type already exists
        raise ProblemException(
            409,
            "Conflict",
            f"an active job of type '{body.job_type}' already exists",
        )
    response.headers["Location"] = f"/v1/jobs/{job_id}"
    return CreateJobResponse(job_id=job_id, status="queued")


@router.get(
    "/v1/jobs/{job_id}",
    response_model=JobResponse,
    tags=["jobs"],
    summary="Fetch a job",
)
def get_job(
    job_id: int,
    principal: Principal = Depends(require_read),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
) -> JobResponse:
    job = store.get_job(job_id)
    creator_sub = job.created_by.sub if (job and job.created_by) else None
    # Hide existence of jobs the caller may not read: 404 rather than 403.
    if job is None or not can_read_job_created_by(principal, settings, creator_sub):
        raise ProblemException(404, "Not Found", f"job {job_id} not found")
    return _to_response(job)


@router.get("/healthz", tags=["ops"], summary="Liveness probe")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["ops"], summary="Readiness probe")
def readyz(store: Store = Depends(get_store)) -> dict[str, str]:
    # Ready only if the datastore is reachable.
    try:
        store.get_job(0)
    except Exception as err:  # noqa: BLE001 - report not-ready, don't crash the probe
        raise ProblemException(503, "Service Unavailable", "datastore not reachable") from err
    return {"status": "ready"}


def _to_response(job: Job) -> JobResponse:
    created_by = job.created_by
    return JobResponse(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        input_payload=job.input_payload,
        payload=job.payload,
        attempts=job.attempts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        created_by=CreatedBy(
            sub=created_by.sub if created_by else None,
            type=created_by.type if created_by else None,
            client_id=created_by.client_id if created_by else None,
        ),
    )
