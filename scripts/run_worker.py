"""Scheduler worker entry point — runs OUTSIDE the API process (spec §16).

Usage:

    # process at most one eligible task (scripted operation / cron / tests)
    .venv/bin/python scripts/run_worker.py --once

    # continuous loop until SIGTERM/SIGINT or max tasks (graceful shutdown:
    # the in-flight task completes before exit)
    .venv/bin/python scripts/run_worker.py --loop --max-tasks 1000

The worker needs the database (``AIC_DATABASE_URL``) but NOT the API
server. Apply migrations first: ``.venv/bin/alembic upgrade head``. While
the infrastructure emergency stop is engaged the worker refuses to claim
new work and logs one idle line; raising the stop resumes processing.
"""

import argparse
import logging

from commons_core.config.settings import get_settings
from commons_core.config.validation import validate_production_startup
from commons_core.database.db import Database
from commons_core.infrastructure.logging import configure_logging
from commons_core.scheduler.worker import run_worker


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, build the database and run the worker loop."""
    parser = argparse.ArgumentParser(
        prog="run_worker.py", description="AIC durable scheduler worker (spec §16)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true", help="process at most one eligible task, then exit"
    )
    mode.add_argument(
        "--loop", action="store_true", help="run continuously until stopped (default)"
    )
    parser.add_argument("--max-tasks", type=int, default=100, help="task cap per loop run")
    parser.add_argument("--lease-seconds", type=int, default=30, help="execution lease duration")
    parser.add_argument(
        "--idle-sleep", type=float, default=0.05, help="seconds to sleep between idle polls"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    # Fail-closed startup: the worker never decrypts content (least privilege
    # — it receives NO AIC_MASTER_KEYS by design), but production still
    # refuses an unprotected SQLite datastore.
    validate_production_startup(settings, require_keys=False)
    configure_logging(settings)
    database = Database(settings.database_url)
    logger = logging.getLogger(__name__)
    logger.info(
        "worker starting mode=%s database=%s lease_seconds=%s task_timeout_seconds=%s",
        "once" if args.once else "loop",
        "configured",
        args.lease_seconds,
        settings.task_timeout_seconds,
    )
    try:
        processed = run_worker(
            database.session,
            once=args.once,
            max_tasks=args.max_tasks,
            lease_seconds=args.lease_seconds,
            idle_sleep=args.idle_sleep,
            task_timeout_seconds=float(settings.task_timeout_seconds),
        )
    finally:
        database.dispose()
    logger.info("worker finished processed=%s", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
