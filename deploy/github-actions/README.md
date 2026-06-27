# GitHub Actions -> ReleasePulse

Tells ReleasePulse that a deployment happened, so it can run its before/after
regression check on the right release. This is the CI entry point of the platform:
the same webhook your deploy tooling already fires, driven from a real pipeline.

## Pieces
- `scripts/notify_releasepulse.sh` - POSTs the deployment webhook. Reads GitHub's
  standard context (`GITHUB_REPOSITORY`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`,
  `GITHUB_SHA`, `GITHUB_REF_NAME`) from the environment.
- `.github/workflows/notify-releasepulse.yml` - runs the script on a published
  release or manual dispatch.

## Configure (repo settings)
| Kind | Name | Value |
|------|------|-------|
| Secret | `RELEASEPULSE_URL` | base URL of the ReleasePulse API (e.g. `https://releasepulse.example.com`) |
| Secret | `RELEASEPULSE_WEBHOOK_SECRET` | the webhook bearer secret |
| Variable | `RELEASEPULSE_SERVICE` | registered service name (defaults to the repo name) |

If the URL/secret are absent the script **skips quietly** (exit 0), so adding the
workflow never breaks a build that hasn't onboarded yet.

## Idempotency
`external_id` is `repo:run_id:run_attempt`. The webhook dedupes on
`(source, external_id)`, so re-running the same CI job re-sends the same id and
ReleasePulse returns the existing deployment instead of creating a duplicate
(no duplicate detector run, incident, or alert).

## Using it in a real app repo
Make notify the final step of your deploy job so it only fires on success:

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - # ... your deploy steps ...
  notify-releasepulse:
    needs: [deploy]
    if: success()
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - env:
          RELEASEPULSE_URL: ${{ secrets.RELEASEPULSE_URL }}
          RELEASEPULSE_WEBHOOK_SECRET: ${{ secrets.RELEASEPULSE_WEBHOOK_SECRET }}
          RELEASEPULSE_SERVICE: ${{ vars.RELEASEPULSE_SERVICE }}
        run: bash scripts/notify_releasepulse.sh
```

## Reachability
GitHub runners must be able to reach `RELEASEPULSE_URL`. For a public/Phase-1
deployment that is just the service URL. For a laptop stack, expose it with a
tunnel (e.g. cloudflared/ngrok) and point `RELEASEPULSE_URL` at the tunnel.

## Validate locally (no GitHub needed)
With the Compose stack up and `demo-svc` registered, stand in for the GitHub
context and run the same script the workflow runs:

```bash
RELEASEPULSE_URL=http://localhost:8000 \
RELEASEPULSE_WEBHOOK_SECRET=dev-webhook-secret \
RELEASEPULSE_SERVICE=demo-svc \
GITHUB_REPOSITORY=acme/app GITHUB_RUN_ID=42 GITHUB_RUN_ATTEMPT=1 GITHUB_SHA=abc123 \
bash scripts/notify_releasepulse.sh
```

First run prints `HTTP 201` (deployment created); a second identical run prints
`HTTP 200` (idempotent - same `external_id`, no duplicate).
