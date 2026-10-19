"""Scheduler worker entry point — runs OUTSIDE the API process (spec §16).

Usage:

    # process at most one eligible task (scripted operation / cron / tests)
    .venv/bin/python scripts/run_worker.py --once

    # continuous loop, UNLIMITED tasks, until SIGTERM/SIGINT or fatal
    # failure (graceful shutdown: the in-flight task completes before exit;
    # the default idle poll is 1s — see --idle-sleep)
    .venv/bin/python scripts/run_worker.py --loop

    # loop with an EXPLICIT task cap (tests / scripted batches)
    .venv/bin/python scripts/run_worker.py --loop --max-tasks 1000

There is NO default task cap: omitting --max-tasks in --loop mode runs the
worker for its whole lifetime (the Railway semantics). --max-tasks is a
loop-only bound and must be a positive integer; combining it with --once is
rejected (argparse error), as are non-positive or negative values.

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
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help=(
            "explicit positive cap on tasks processed in --loop mode "
            "(default: unlimited — run until shutdown signal)"
        ),
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=90,
        help=(
            "execution lease duration (default: 90 — MUST exceed "
            "AIC_TASK_TIMEOUT_SECONDS, whose default is 30; the worker "
            "refuses to start otherwise)"
        ),
    )
    parser.add_argument(
        "--idle-sleep",
        type=float,
        default=1.0,
        help="seconds to sleep between idle polls (default: 1.0 — one DB poll/second while idle)",
    )
    args = parser.parse_args(argv)

    # Fail-closed argument validation: an invalid explicit task limit (or a
    # contradictory mode combination) is REJECTED with a clean usage error
    # instead of being silently accepted (exit code 2, message on stderr).
    if args.max_tasks is not None and args.max_tasks < 1:
        parser.error(f"--max-tasks must be a positive integer (got {args.max_tasks})")
    if args.once and args.max_tasks is not None:
        parser.error(
            "--max-tasks bounds --loop runs only: --once already processes at most one task"
        )
    if args.idle_sleep < 0:
        parser.error(f"--idle-sleep must be >= 0 (got {args.idle_sleep})")

    settings = get_settings()
    # Fail-closed startup: the worker never decrypts content (least privilege
    # — it receives NO AIC_MASTER_KEYS by design), but production still
    # refuses an unprotected SQLite datastore.
    validate_production_startup(settings, require_keys=False)
    configure_logging(settings)
    database = Database(settings.database_url)
    # Logger UNDER the commons_core tree: configure_logging() attaches its
    # privacy-preserving stderr handler to that tree only. A bare
    # ``getLogger(__name__)`` here resolves to ``__main__`` when this file
    # runs as a script — OUTSIDE the configured tree — so every start/finish
    # line would be silently dropped, leaving a long-running production
    # worker with no lifecycle visibility in its logs.
    logger = logging.getLogger("commons_core.scripts.run_worker")
    logger.info(
        "worker starting mode=%s database=%s lease_seconds=%s task_timeout_seconds=%s "
        "task_cap=%s idle_sleep=%s",
        "once" if args.once else "loop",
        "configured",
        args.lease_seconds,
        settings.task_timeout_seconds,
        "unlimited" if args.max_tasks is None else args.max_tasks,
        args.idle_sleep,
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
