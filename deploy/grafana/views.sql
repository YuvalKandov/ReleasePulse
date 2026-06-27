-- Read layer for the Grafana service-health dashboard.
-- Plain views over the app tables: no Grafana macros here, so this file is valid
-- standalone SQL and is exercised by tests/test_grafana_views.py. The dashboard
-- panels add the time macros ($__timeFilter / $__timeGroup) when they query these.

CREATE OR REPLACE VIEW service_check_metrics_view AS
SELECT
    c.checked_at,
    s.id   AS service_id,
    s.name AS service_name,
    e.id   AS endpoint_id,
    e.url  AS endpoint_url,
    e.environment,
    c.success,
    c.latency_ms,
    c.status_code,
    c.error_type
FROM checks c
JOIN endpoints e ON e.id = c.endpoint_id
JOIN services  s ON s.id = e.service_id;

CREATE OR REPLACE VIEW deployment_annotations_view AS
SELECT
    d.id                    AS deployment_id,
    d.effective_deployed_at AS deployed_at,
    s.name                  AS service_name,
    d.environment,
    COALESCE(d.version, d.commit_sha, 'unknown') AS release,
    d.evaluation_status,
    d.evaluation_reason
FROM deployments d
JOIN services s ON s.id = d.service_id;
