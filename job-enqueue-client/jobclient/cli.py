"""CLI: ``python main.py <job_type> [payload_json]``."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace

from jobclient.client import EnqueueError, JobClient
from jobclient.config import Config, ConfigError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-enqueue",
        description="Enqueue one job by POSTing {job_type, payload} to the jobs API.",
    )
    parser.add_argument("job_type", help="job type to enqueue, e.g. settlement")
    parser.add_argument(
        "payload",
        nargs="?",
        help='optional JSON object of payload overrides, e.g. \'{"region": "eu"}\'',
    )
    parser.add_argument("--api-url", help="base URL of the jobs API (env: JOB_API_URL)")
    parser.add_argument(
        "--scope",
        action="append",
        dest="scopes",
        metavar="SCOPE",
        help="scope to put in the minted token; repeatable (env: JOB_API_SCOPES)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        help="total attempts including the first (env: JOB_API_MAX_ATTEMPTS)",
    )
    return parser


def _parse_payload(raw: str | None) -> dict:
    if raw is None:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    return payload


def _resolve_config(args: argparse.Namespace) -> Config:
    config = Config.from_env()
    overrides: dict = {}
    if args.api_url:
        overrides["api_url"] = args.api_url
    if args.scopes:
        overrides["scopes"] = tuple(args.scopes)
    if args.max_attempts is not None:
        if args.max_attempts < 1:
            raise ConfigError("--max-attempts must be >= 1")
        overrides["max_attempts"] = args.max_attempts
    return replace(config, **overrides) if overrides else config


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = _parse_payload(args.payload)
        config = _resolve_config(args)
    except (ValueError, ConfigError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_USAGE

    try:
        result = JobClient(config).enqueue(args.job_type, payload)
    except EnqueueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_FAILED

    if result.duplicate:
        sys.stdout.write(f"skipped {args.job_type}: an active job already exists\n")
        return EXIT_OK
    if result.status_code >= 400:
        sys.stderr.write(
            f"error: API rejected {args.job_type} with HTTP {result.status_code}: "
            f"{json.dumps(result.body)}\n"
        )
        return EXIT_FAILED

    job_id = result.job_id
    suffix = f" as job {job_id}" if job_id is not None else ""
    sys.stdout.write(f"enqueued {args.job_type}{suffix} (attempts: {result.attempts})\n")
    return EXIT_OK
