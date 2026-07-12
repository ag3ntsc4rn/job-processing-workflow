#!/usr/bin/env bash
# Minimal AutoSys-style producer: fetch a client-credentials token from Ping
# Federate, then POST a job to the handler API. No enqueue SQL is duplicated —
# this just calls the API, which owns the jobs+outbox+dedup transaction.
#
# Usage:
#   OIDC_TOKEN_URL=https://ping.example.com/as/token.oauth2 \
#   CLIENT_ID=autosys-svc CLIENT_SECRET=*** \
#   API_URL=https://job-api.example.com \
#   scripts/enqueue_job.sh settlement '{"batch_size": 25}'
#
# Env:
#   OIDC_TOKEN_URL   Ping token endpoint
#   CLIENT_ID        service client id
#   CLIENT_SECRET    service client secret
#   API_URL          base URL of the handler API
#   SCOPE            requested scope (default: jobs.write)
set -euo pipefail

JOB_TYPE="${1:?usage: enqueue_job.sh <job_type> [payload_json]}"
PAYLOAD="${2:-}"
SCOPE="${SCOPE:-jobs.write}"

token=$(curl -fsS -u "${CLIENT_ID}:${CLIENT_SECRET}" \
  -d "grant_type=client_credentials" \
  -d "scope=${SCOPE}" \
  "${OIDC_TOKEN_URL}" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

if [[ -n "${PAYLOAD}" ]]; then
  body=$(python3 -c "import sys,json; print(json.dumps({'job_type': sys.argv[1], 'payload': json.loads(sys.argv[2])}))" "${JOB_TYPE}" "${PAYLOAD}")
else
  body=$(python3 -c "import sys,json; print(json.dumps({'job_type': sys.argv[1]}))" "${JOB_TYPE}")
fi

curl -fsS -X POST "${API_URL}/v1/jobs" \
  -H "Authorization: Bearer ${token}" \
  -H "Content-Type: application/json" \
  -d "${body}"
echo
