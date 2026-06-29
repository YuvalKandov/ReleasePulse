{{/*
Named template helpers. These are not Kubernetes objects (the leading "_" and the
.tpl extension tell Helm not to render this file as a manifest); they are reusable
snippets the real templates call with `include`.
*/}}

{{/* Short name of the chart. */}}
{{- define "releasepulse.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified base name for all resources: "<release>-releasepulse", unless the
release name already contains the chart name (avoids "releasepulse-releasepulse").
*/}}
{{- define "releasepulse.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Common labels stamped on every object (the recommended Kubernetes set). */}}
{{- define "releasepulse.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "releasepulse.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* The stable subset used to match Pods to their controller/Service. */}}
{{- define "releasepulse.selectorLabels" -}}
app.kubernetes.io/name: {{ include "releasepulse.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Stable names for the shared config/secret objects and the postgres host. */}}
{{- define "releasepulse.configName" -}}{{ include "releasepulse.fullname" . }}-config{{- end -}}
{{- define "releasepulse.appSecretName" -}}{{ include "releasepulse.fullname" . }}-app{{- end -}}
{{- define "releasepulse.dbSecretName" -}}{{ include "releasepulse.fullname" . }}-db{{- end -}}
{{- define "releasepulse.postgresHost" -}}{{ include "releasepulse.fullname" . }}-postgres{{- end -}}

{{/*
The SQLAlchemy URL. Built from the in-cluster Postgres values, or taken verbatim
from externalDatabaseUrl when postgres.enabled=false. Lives in the Secret because
it embeds the password.
*/}}
{{- define "releasepulse.databaseUrl" -}}
{{- if .Values.postgres.enabled -}}
postgresql+psycopg://{{ .Values.postgres.user }}:{{ .Values.postgres.password }}@{{ include "releasepulse.postgresHost" . }}:5432/{{ .Values.postgres.database }}
{{- else -}}
{{- required "externalDatabaseUrl is required when postgres.enabled=false" .Values.externalDatabaseUrl -}}
{{- end -}}
{{- end -}}

{{/*
An init container that blocks until the in-cluster Postgres accepts connections.
Reused by the migrate and db-provision Jobs so neither races the database coming up.
Uses the postgres image because it ships pg_isready.
*/}}
{{- define "releasepulse.waitForPostgres" -}}
- name: wait-for-postgres
  image: {{ .Values.postgres.image | quote }}
  command:
    - sh
    - -c
    - until pg_isready -h {{ include "releasepulse.postgresHost" . }} -U {{ .Values.postgres.user }} >/dev/null 2>&1; do echo "waiting for postgres..."; sleep 2; done
{{- end -}}

{{/*
An init container that blocks until Postgres is reachable AND the schema is
migrated (the alembic_version table exists). Used by api/worker so they never
start against an empty database - the worker would otherwise crash on its first
reconcile. Needs the DB password, so it reads PGPASSWORD from the db Secret.
*/}}
{{- define "releasepulse.waitForMigrations" -}}
- name: wait-for-migrations
  image: {{ .Values.postgres.image | quote }}
  command:
    - sh
    - -c
    - >-
      until pg_isready -h {{ include "releasepulse.postgresHost" . }} -U {{ .Values.postgres.user }} >/dev/null 2>&1; do echo "waiting for postgres..."; sleep 2; done;
      until [ "$(psql -h {{ include "releasepulse.postgresHost" . }} -U {{ .Values.postgres.user }} -d {{ .Values.postgres.database }} -tAc "SELECT to_regclass('public.alembic_version')")" = "alembic_version" ]; do echo "waiting for migrations..."; sleep 2; done
  env:
    - name: PGPASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ include "releasepulse.dbSecretName" . }}
          key: POSTGRES_PASSWORD
{{- end -}}
