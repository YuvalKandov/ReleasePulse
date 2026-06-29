# Runbook: restore the database from a backup

**When to use this:** the Postgres PVC is lost or corrupted, a migration or bulk
operation damaged data, or you need to verify that backups are actually
restorable (do this periodically, not only during an incident).

Backups are written daily at 02:00 by the `releasepulse-backup` CronJob: a
`pg_dump -Fc` (custom compressed format) shipped to the MinIO bucket `backups`.
Retention inside MinIO is the `backup.keep` window (default 30 days).

There are two procedures below:
1. **Verify restore** (non-destructive) - restore into a scratch database and
   check it. Run this on a schedule to keep the backup trustworthy.
2. **Disaster restore** (destructive) - replace the live database.

---

## 0. List available backups

Pull the MinIO root credentials from the Secret and list the bucket from a
throwaway `mc` pod (`MC_HOST_local` configures the alias from one env var):

```bash
U=$(kubectl -n releasepulse get secret releasepulse-minio -o jsonpath='{.data.MINIO_ROOT_USER}' | base64 -d)
P=$(kubectl -n releasepulse get secret releasepulse-minio -o jsonpath='{.data.MINIO_ROOT_PASSWORD}' | base64 -d)

kubectl -n releasepulse run mc-ls --rm -i --restart=Never \
  --image=minio/mc:RELEASE.2025-04-16T18-13-26Z \
  --env="MC_HOST_local=http://${U}:${P}@releasepulse-minio:9000" \
  -- mc ls local/backups/
```

Note the filename you want to restore, e.g. `db-20260629T020000Z.dump`.

---

## 1. Verify restore (non-destructive)

Restores the chosen dump into a scratch database `releasepulse_restore` on the
same Postgres instance and prints row counts + the schema version. It never
touches the live `releasepulse` database.

Replace `REPLACE_ME.dump` with the filename from step 0, then apply:

```bash
kubectl -n releasepulse apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: db-restore-verify
  namespace: releasepulse
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      initContainers:
        - name: fetch
          image: minio/mc:RELEASE.2025-04-16T18-13-26Z
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              mc alias set local "http://releasepulse-minio:9000" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
              mc cp "local/backups/$DUMP" /work/restore.dump
          env:
            - { name: DUMP, value: "REPLACE_ME.dump" }
            - { name: MINIO_ROOT_USER, valueFrom: { secretKeyRef: { name: releasepulse-minio, key: MINIO_ROOT_USER } } }
            - { name: MINIO_ROOT_PASSWORD, valueFrom: { secretKeyRef: { name: releasepulse-minio, key: MINIO_ROOT_PASSWORD } } }
          volumeMounts:
            - { name: work, mountPath: /work }
      containers:
        - name: restore
          image: postgres:17
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              dropdb --if-exists -h "$PGHOST" -U "$PGUSER" releasepulse_restore
              createdb -h "$PGHOST" -U "$PGUSER" releasepulse_restore
              pg_restore --no-owner --no-acl -h "$PGHOST" -U "$PGUSER" -d releasepulse_restore /work/restore.dump
              echo "=== row counts ==="
              psql -h "$PGHOST" -U "$PGUSER" -d releasepulse_restore -c \
                "SELECT 'services' AS t, count(*) FROM services
                 UNION ALL SELECT 'endpoints', count(*) FROM endpoints
                 UNION ALL SELECT 'checks', count(*) FROM checks
                 UNION ALL SELECT 'deployments', count(*) FROM deployments;"
              echo "=== schema version ==="
              psql -h "$PGHOST" -U "$PGUSER" -d releasepulse_restore -c "SELECT version_num FROM alembic_version;"
          env:
            - { name: PGHOST, value: releasepulse-postgres }
            - { name: PGUSER, value: postgres }
            - { name: PGPASSWORD, valueFrom: { secretKeyRef: { name: releasepulse-db, key: POSTGRES_PASSWORD } } }
          volumeMounts:
            - { name: work, mountPath: /work }
      volumes:
        - { name: work, emptyDir: {} }
EOF

kubectl -n releasepulse wait --for=condition=complete job/db-restore-verify --timeout=120s
kubectl -n releasepulse logs job/db-restore-verify
```

**Pass criteria:** the job completes, `pg_restore` reports no errors, the row
counts look sane for the chosen backup, and `alembic_version` shows the expected
revision (must match `alembic heads` for the code running in the cluster).

Clean up the scratch database and the job:

```bash
kubectl -n releasepulse delete job db-restore-verify
kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
  psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS releasepulse_restore;"
```

---

## 2. Disaster restore (destructive - replaces live data)

Only after a verify restore (step 1) has confirmed the dump is good.

**a. Stop writers** so nothing races the restore:

```bash
kubectl -n releasepulse scale deploy/releasepulse-api deploy/releasepulse-worker --replicas=0
```

**b. Restore into the live database.** Reuse the step-1 Job manifest with two
changes: name it `db-restore-live`, and change the restore container's `args`
to drop and recreate the live `releasepulse` database instead of the scratch one:

```sh
set -e
dropdb --if-exists -h "$PGHOST" -U "$PGUSER" releasepulse
createdb -h "$PGHOST" -U "$PGUSER" releasepulse
pg_restore --no-owner --no-acl -h "$PGHOST" -U "$PGUSER" -d releasepulse /work/restore.dump
```

(`dropdb` requires no open connections - that's why step (a) scales writers to
zero. If it still fails, terminate sessions with
`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='releasepulse' AND pid<>pg_backend_pid();`.)

**c. Confirm the schema version** matches the running code:

```bash
kubectl -n releasepulse exec -i sts/releasepulse-postgres -- \
  psql -U postgres -d releasepulse -c "SELECT version_num FROM alembic_version;"
```

If the dump predates the current code's migrations, run `alembic upgrade head`
(trigger the `migrate` Job, or exec it in an api pod) to bring the schema
forward before bringing the app back.

**d. Restart writers:**

```bash
kubectl -n releasepulse scale deploy/releasepulse-api deploy/releasepulse-worker --replicas=1
```

**e. Verify the loop recovers:** the worker `/readyz` goes green within a couple
of reconcile cycles and new `checks` rows start appearing (see
[worker-not-producing-checks.md](worker-not-producing-checks.md)).

---

## Notes

- `-Fc` dumps restore with `pg_restore`, not `psql`. `--no-owner --no-acl` skips
  the original role/ownership grants so the restore does not depend on the
  `sentinel_grafana_ro` role existing first; the `db-provision` Job recreates
  that role and the Grafana views on the next ArgoCD sync.
- The backup captures `alembic_version`, so a restore lands at the schema version
  that was live when the dump ran - always reconcile it against `alembic heads`.
- MinIO here is in-cluster on its own PVC (off the Postgres PVC). In production,
  point `mc`/the backup job at real S3; the procedure is otherwise identical.

**Last tested:** 2026-06-29 - step 1 verified end to end on the kind cluster
(dump fetched from MinIO, `pg_restore` clean, `alembic_version` restored intact).
