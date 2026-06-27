#!/usr/bin/env bash
# Notify ReleasePulse that a deployment succeeded - meant as the last step of a
# deploy job (GitHub Actions or any CI). Reads GitHub's standard context vars from
# the environment, so locally you can stand in for them (see deploy/github-actions/README.md).
#
# Safe in a pipeline that hasn't configured ReleasePulse: if the URL/secret are
# unset it skips with a notice instead of failing the build.
set -euo pipefail

: "${RELEASEPULSE_SERVICE:?set RELEASEPULSE_SERVICE to the registered service name}"

if [[ -z "${RELEASEPULSE_URL:-}" || -z "${RELEASEPULSE_WEBHOOK_SECRET:-}" ]]; then
  echo "notify-releasepulse: RELEASEPULSE_URL / RELEASEPULSE_WEBHOOK_SECRET not set; skipping."
  exit 0
fi

repo="${GITHUB_REPOSITORY:-local/manual}"
run_id="${GITHUB_RUN_ID:-0}"
run_attempt="${GITHUB_RUN_ATTEMPT:-1}"
sha="${GITHUB_SHA:-unknown}"
version="${RELEASEPULSE_VERSION:-${GITHUB_REF_NAME:-$sha}}"

# The webhook dedupes on (source, external_id) - spec 7. One deploy is one
# (repo, run, attempt), so a re-run of the same CI job never double-counts.
external_id="${repo}:${run_id}:${run_attempt}"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

payload="$(cat <<JSON
{"service":"${RELEASEPULSE_SERVICE}","source":"github-actions","external_id":"${external_id}","version":"${version}","commit_sha":"${sha}","reported_deployed_at":"${now}"}
JSON
)"

echo "notify-releasepulse: POST ${RELEASEPULSE_URL}/webhooks/deployments  external_id=${external_id}"

resp="$(mktemp)"
code="$(curl -sS -o "${resp}" -w '%{http_code}' \
  -X POST "${RELEASEPULSE_URL}/webhooks/deployments" \
  -H "Authorization: Bearer ${RELEASEPULSE_WEBHOOK_SECRET}" \
  -H "Content-Type: application/json" \
  -d "${payload}")"

echo "notify-releasepulse: HTTP ${code}"
cat "${resp}"; echo
rm -f "${resp}"

case "${code}" in
  2*) echo "notify-releasepulse: ok" ;;
  *)  echo "notify-releasepulse: failed (HTTP ${code})" >&2; exit 1 ;;
esac
