"""AIC restore drill CLI — content-blind synthetic verification of a backup.

Usage (from the repository root, against a disposable local server)::

    python scripts/restore_drill.py \\
        --server-url postgresql+psycopg://commons:secret@127.0.0.1:5432/postgres \\
        --backup-id aic-20261019T061234Z-ab12cd34 \\
        --identity-file /path/to/offline-test-identity \\
        --storage-dir /path/to/backup-root \\
        [--expected-facts expected.json] [--with-application-keys] \\
        [--allow-db NAME] [--report-json report.json]

Fail-closed destination safety (Stage 1 — docs/BACKUP.md):

* the drill creates a uniquely named disposable database
  (``aic_drill_<hex>``) and drops it afterwards — success and failure
  paths alike;
* server hosts must be loopback/private unless
  ``--allow-nonlocal-destination`` is passed (an explicit, separately
  reviewed escape hatch recorded in the report);
* hosts matching Railway-ish patterns are refused outright (no override);
* a target database that already exists is refused — unique names only;
* there is NO production restore command; this tool validates backups,
  it does not rebuild production.

Application-encryption demonstrations (when --with-application-keys is
set, keys come from AIC_MASTER_KEYS/AIC_ACTIVE_KEY_ID — never argv):

1. restoring the database alone leaves content encrypted;
2. the correct master key decrypts it byte-for-byte;
3. a wrong master key fails;
4. tampered envelopes are rejected by the AEAD tag.

Exit codes: 0 drill PASS; 1 drill FAIL or error (target still cleaned up);
2 configuration error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from commons_core.backup import (  # noqa: E402
    DrillFailure,
    LocalFilesystemStorage,
    ToolError,
    run_restore_drill,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="restore_drill.py",
        description=(
            "Verify a published backup by restoring it into a disposable "
            "database and running the content-blind application battery "
            "(synthetic data only — Stage 1)."
        ),
    )
    parser.add_argument(
        "--server-url",
        required=True,
        help="URL of a disposable PostgreSQL server INCLUDING its maintenance "
        "database (e.g. …/postgres); the drill creates and drops a unique "
        "target database here",
    )
    parser.add_argument("--backup-id", required=True, help="published backup id to drill")
    parser.add_argument(
        "--identity-file",
        required=True,
        help="path to the age identity (AGE-SECRET-KEY-1… file) able to decrypt "
        "this backup — for Stage 1 this is a runtime-generated SYNTHETIC test "
        "identity, never a committed key",
    )
    parser.add_argument(
        "--storage-dir",
        required=True,
        help="root of the LocalFilesystemStorage adapter holding the backup",
    )
    parser.add_argument(
        "--expected-facts",
        default=None,
        help="JSON of controlled synthetic expectations produced by the source "
        "generator (tests/operational/backup_synth.py); without it, only "
        "schema-level checks run",
    )
    parser.add_argument(
        "--with-application-keys",
        action="store_true",
        help="run application-envelope checks using AIC_MASTER_KEYS / "
        "AIC_ACTIVE_KEY_ID from the environment (keys are never accepted on "
        "the command line)",
    )
    parser.add_argument(
        "--database-name",
        default=None,
        help="explicit disposable target database name (must pass the "
        "disposable-name guard or --allow-db); default: unique aic_drill_<hex>",
    )
    parser.add_argument(
        "--allow-db",
        action="append",
        default=[],
        metavar="NAME",
        help="explicitly allowlist one target database name (repeatable; "
        "requires the same separate review as any production-looking name)",
    )
    parser.add_argument(
        "--allow-nonlocal-destination",
        action="store_true",
        help="permit restoring toward a non-loopback/private host (explicit, "
        "separately reviewed override; recorded in the report)",
    )
    parser.add_argument(
        "--keep-database",
        action="store_true",
        help="debugging: keep the disposable target database after the drill "
        "(recorded in the report; you must drop it manually)",
    )
    parser.add_argument(
        "--report-json",
        default=None,
        help="write the machine-readable drill report to this path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import os

    parser = build_parser()
    args = parser.parse_args(argv)

    master_keys = None
    active_key_id = None
    if args.with_application_keys:
        master_keys = os.environ.get("AIC_MASTER_KEYS", "")
        active_key_id = os.environ.get("AIC_ACTIVE_KEY_ID", "") or None
        if not master_keys:
            print(
                "refusing to run: --with-application-keys set but AIC_MASTER_KEYS "
                "is not present in the environment — fail-closed",
                file=sys.stderr,
            )
            return 2

    storage = LocalFilesystemStorage(args.storage_dir)
    try:
        report = run_restore_drill(
            server_url=args.server_url,
            storage=storage,
            backup_id=args.backup_id,
            identity_path=args.identity_file,
            expected_facts_path=args.expected_facts,
            master_keys=master_keys,
            active_key_id=active_key_id,
            database_name=args.database_name,
            allow_nonlocal=args.allow_nonlocal_destination,
            allowed_db_names=tuple(args.allow_db),
            keep_database=args.keep_database,
        )
    except (DrillFailure, ToolError) as exc:
        print(f"DRILL-FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — never claim success on surprises
        print(f"DRILL-ERROR ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1

    if args.report_json:
        Path(args.report_json).write_text(report.to_json(), encoding="utf-8")
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
