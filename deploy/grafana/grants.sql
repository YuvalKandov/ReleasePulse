-- Dedicated read-only role for Grafana (spec 10): it may SELECT the two views and
-- nothing else - not the raw tables, never the app's credentials. A view runs with
-- its owner's privileges, so granting SELECT on the views alone is enough to read
-- the underlying data while `SELECT * FROM checks` stays denied. Idempotent.
-- Dev-only password; production wiring (K8s secrets) comes in Phase 1.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_grafana_ro') THEN
        CREATE ROLE sentinel_grafana_ro LOGIN PASSWORD 'grafana-ro';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE releasepulse TO sentinel_grafana_ro;
GRANT USAGE ON SCHEMA public TO sentinel_grafana_ro;
GRANT SELECT ON service_check_metrics_view, deployment_annotations_view TO sentinel_grafana_ro;
