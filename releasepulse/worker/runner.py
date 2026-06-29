"""The check worker: schedule per-endpoint HTTP checks and persist results.

Single replica (per spec). APScheduler fires one job per enabled endpoint at its
interval; a periodic reconcile keeps the job set in sync with the database so
newly registered or removed endpoints are picked up without a restart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from releasepulse.alerting.dispatch import dispatch_pending_alerts
from releasepulse.alerting.sender import AlertSender, TelegramAlertSender
from releasepulse.config import Settings, get_settings
from releasepulse.db import get_sessionmaker
from releasepulse.detector.core import Thresholds
from releasepulse.detector.service import evaluate_due_deployments
from releasepulse.metrics import check_duration_seconds, checks_total
from releasepulse.models import Check, Endpoint
from releasepulse.security.ssrf import Resolver, resolve_host
from releasepulse.worker.check import perform_check
from releasepulse.worker.http import WorkerState, build_server, create_app

logger = logging.getLogger(__name__)

RECONCILE_JOB_ID = "reconcile"
RECONCILE_INTERVAL_SEC = 60
EVALUATE_JOB_ID = "evaluate_due"
EVALUATE_INTERVAL_SEC = 30
ALERT_JOB_ID = "dispatch_alerts"
ALERT_INTERVAL_SEC = 30
PERIODIC_JOB_IDS = frozenset({RECONCILE_JOB_ID, EVALUATE_JOB_ID, ALERT_JOB_ID})

# Readiness goes stale if reconcile hasn't bumped the heartbeat in this long.
# Three reconcile intervals tolerates one slow/missed run before flagging - tight
# enough to catch a wedged loop, loose enough not to flap on a single hiccup.
WORKER_HEARTBEAT_MAX_AGE_SEC = RECONCILE_INTERVAL_SEC * 3


async def check_and_record(
    session: Session,
    endpoint: Endpoint,
    client: httpx.AsyncClient,
    *,
    app_env: str,
    allowlist_raw: str,
    resolver: Resolver = resolve_host,
) -> Check:
    """Run one check and persist it as a checks row. Returns the Check."""
    result = await perform_check(
        endpoint, client, app_env=app_env, allowlist_raw=allowlist_raw, resolver=resolver
    )
    checks_total.labels(result="success" if result.success else "failure").inc()
    if result.latency_ms is not None:
        # latency_ms is absent for failures with no response (e.g. timeout, DNS);
        # only observe a real measurement. Histogram is in seconds, our metric is ms.
        check_duration_seconds.observe(result.latency_ms / 1000)
    check = Check(
        endpoint_id=endpoint.id,
        checked_at=datetime.now(timezone.utc),
        success=result.success,
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        error_type=result.error_type,
        error_detail=result.error_detail,
    )
    session.add(check)
    session.commit()
    return check


async def _run_check(
    endpoint_id: UUID,
    client: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
    app_env: str,
    allowlist_raw: str,
) -> None:
    """Scheduler job: load the endpoint, check it, record the result."""
    with session_factory() as session:
        endpoint = session.get(Endpoint, endpoint_id)
        if endpoint is None or not endpoint.enabled or endpoint.deleted_at is not None:
            return  # registration changed since scheduling; the next reconcile will drop it
        check = await check_and_record(
            session, endpoint, client, app_env=app_env, allowlist_raw=allowlist_raw
        )
        logger.info(
            "checked %s success=%s status=%s latency=%sms error=%s",
            endpoint.url, check.success, check.status_code, check.latency_ms, check.error_type,
        )


def reconcile(
    scheduler: AsyncIOScheduler,
    session_factory: sessionmaker[Session],
    client: httpx.AsyncClient,
    app_env: str,
    allowlist_raw: str,
) -> None:
    """Add jobs for newly enabled endpoints and remove jobs for gone ones.

    Leaves existing jobs untouched so their check cadence isn't disturbed.
    """
    with session_factory() as session:
        endpoints = session.scalars(
            select(Endpoint).where(
                Endpoint.enabled.is_(True), Endpoint.deleted_at.is_(None)
            )
        ).all()
        wanted = {str(e.id): (e.id, e.check_interval_sec) for e in endpoints}

    # Only endpoint jobs are reconciled; the periodic jobs (evaluate_due,
    # dispatch_alerts) are not endpoints and must never be swept up here.
    existing = {j.id for j in scheduler.get_jobs() if j.id not in PERIODIC_JOB_IDS}

    for job_id, (endpoint_id, interval) in wanted.items():
        if job_id not in existing:
            scheduler.add_job(
                _run_check,
                IntervalTrigger(seconds=interval),
                args=[endpoint_id, client, session_factory, app_env, allowlist_raw],
                id=job_id,
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc),  # check immediately, then on interval
            )
            logger.info("scheduled checks for endpoint %s every %ss", job_id, interval)

    for job_id in existing - wanted.keys():
        scheduler.remove_job(job_id)
        logger.info("unscheduled endpoint %s", job_id)


def thresholds_from_settings(settings: Settings) -> Thresholds:
    """Build detector windows from config. Only the window/sample knobs are
    configurable; the comparison thresholds keep their per-endpoint-overridable
    defaults (Thresholds())."""
    return Thresholds(
        baseline_window=timedelta(seconds=settings.detector_baseline_sec),
        warmup=timedelta(seconds=settings.detector_warmup_sec),
        observation_window=timedelta(seconds=settings.detector_observation_sec),
        min_samples=settings.detector_min_samples,
        min_successful_baseline=settings.detector_min_successful_baseline,
    )


def evaluate_due(session_factory: sessionmaker[Session], thresholds: Thresholds) -> None:
    """Scheduler job: evaluate every deployment whose window has closed."""
    evaluated = evaluate_due_deployments(session_factory, defaults=thresholds)
    if evaluated:
        logger.info("auto-evaluated %d due deployment(s)", len(evaluated))


async def dispatch_alerts(
    session_factory: sessionmaker[Session], sender: AlertSender
) -> None:
    """Scheduler job: send a tracked alert for every incident that still needs one."""
    with session_factory() as session:
        sent = await dispatch_pending_alerts(session, sender)
    if sent:
        logger.info("dispatched %d alert(s)", sent)


async def main() -> None:
    settings = get_settings()
    session_factory = get_sessionmaker()
    scheduler = AsyncIOScheduler()
    state = WorkerState(scheduler=scheduler, session_factory=session_factory)
    heartbeat_max_age = timedelta(seconds=WORKER_HEARTBEAT_MAX_AGE_SEC)

    async with httpx.AsyncClient(follow_redirects=False) as client:
        def reconcile_and_beat() -> None:
            # Wrap reconcile so a successful run also bumps the heartbeat that
            # /readyz checks. reconcile itself stays pure (and unit-tested) - the
            # heartbeat coupling lives only here, where the loop is wired up.
            reconcile(
                scheduler, session_factory, client, settings.app_env, settings.ssrf_allowlist
            )
            state.beat()

        scheduler.add_job(
            reconcile_and_beat,
            IntervalTrigger(seconds=RECONCILE_INTERVAL_SEC),
            id=RECONCILE_JOB_ID,
        )
        reconcile_and_beat()
        scheduler.add_job(
            evaluate_due,
            IntervalTrigger(seconds=EVALUATE_INTERVAL_SEC),
            args=[session_factory, thresholds_from_settings(settings)],
            id=EVALUATE_JOB_ID,
            max_instances=1,
            coalesce=True,
        )
        if settings.telegram_bot_token and settings.telegram_chat_id:
            sender = TelegramAlertSender(
                client, settings.telegram_bot_token, settings.telegram_chat_id
            )
            scheduler.add_job(
                dispatch_alerts,
                IntervalTrigger(seconds=ALERT_INTERVAL_SEC),
                args=[session_factory, sender],
                id=ALERT_JOB_ID,
                max_instances=1,
                coalesce=True,
            )
        else:
            logger.info("telegram alerting disabled (no bot token/chat id configured)")
        scheduler.start()
        n_endpoints = sum(1 for j in scheduler.get_jobs() if j.id not in PERIODIC_JOB_IDS)
        logger.info("worker started with %d endpoint job(s)", n_endpoints)

        # Internal probe/metrics server, run as a task in this same loop so its
        # /readyz observes the live scheduler and heartbeat.
        server = build_server(
            create_app(state, heartbeat_max_age=heartbeat_max_age),
            host="0.0.0.0",
            port=settings.worker_http_port,
        )
        server_task = asyncio.create_task(server.serve())
        logger.info("worker health server listening on :%d", settings.worker_http_port)

        try:
            await asyncio.Event().wait()  # run until interrupted
        finally:
            server.should_exit = True
            scheduler.shutdown(wait=False)
            await asyncio.gather(server_task, return_exceptions=True)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    run()
