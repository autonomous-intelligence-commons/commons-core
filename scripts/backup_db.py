"""AIC backup CLI — streaming pg_dump → age encryption → verified publication.

Usage (from the repository root, against a disposable/synthetic database)::

    python scripts/backup_db.py \\
        --database-url postgresql+psycopg://commons:secret@127.0.0.1:5432/db \\
        --recipient age1... \\
        --storage-dir /path/to/backup-root

Environment fallbacks (fail-closed — missing values refuse to run):

* ``AIC_BACKUP_DATABASE_URL`` for --database-url
* ``AIC_BACKUP_RECIPIENT``   for --recipient (age PUBLIC key — never an identity)
* ``AIC_BACKUP_STORAGE_DIR`` for --storage-dir

Behaviour (ADR-0009 / docs/BACKUP.md):

* the plaintext dump never exists as a file — pg_dump's custom-format
  output is piped straight into ``age -r RECIPIENT``;
* connection parameters reach pg_dump via libpq environment variables,
  never argv (no credentials in process listings);
* the staged encrypted artifact is verified (SHA-256 + structural age
  header check) before publication; any failure removes it and publishes
  nothing;
* publication is atomic + verified: both objects must verify, then the
  commit marker is written — the ONLY success signal;
* output is scrubbed of credentials (URLs are described without passwords).

Exit codes: 0 success; 1 pipeline failure (nothing published);
2 configuration/preflight error.
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
    BackupError,
    LocalFilesystemStorage,
    PreflightError,
    ToolError,
    describe_url,
    run_backup,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup_db.py",
        description=(
            "Create one encrypted PostgreSQL logical backup with verified "
            "atomic publication (Stage 1 of the backup & recovery plan)."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL to back up (env AIC_BACKUP_DATABASE_URL); the "
        "password never appears in logs or subprocess argv",
    )
    parser.add_argument(
        "--recipient",
        default=None,
        help="age PUBLIC recipient key (age1…) — the backup path refuses "
        "private identities (env AIC_BACKUP_RECIPIENT)",
    )
    parser.add_argument(
        "--storage-dir",
        default=None,
        help="root of the LocalFilesystemStorage adapter (env "
        "AIC_BACKUP_STORAGE_DIR); created with 0700 directories",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full manifest JSON on success (still non-sensitive)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import os

    parser = build_parser()
    args = parser.parse_args(argv)

    database_url = args.database_url or os.environ.get("AIC_BACKUP_DATABASE_URL", "")
    recipient = args.recipient or os.environ.get("AIC_BACKUP_RECIPIENT", "")
    storage_dir = args.storage_dir or os.environ.get("AIC_BACKUP_STORAGE_DIR", "")

    if not database_url or not recipient or not storage_dir:
        print(
            "refusing to run: --database-url/--recipient/--storage-dir (or their "
            "AIC_BACKUP_* environment equivalents) are all required — fail-closed",
            file=sys.stderr,
        )
        return 2

    storage = LocalFilesystemStorage(storage_dir)
    try:
        outcome = run_backup(
            database_url=database_url,
            recipient=recipient,
            storage=storage,
            work_dir=Path(storage_dir) / ".work",
        )
    except (PreflightError, ToolError) as exc:
        print(f"backup PREFLIGHT FAILED (nothing published): {exc}", file=sys.stderr)
        return 2
    except BackupError as exc:
        print(f"backup FAILED (nothing published): {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — never claim success on surprises
        print(
            f"backup FAILED unexpectedly ({type(exc).__name__}; nothing published): {exc}",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(outcome.manifest.to_json())
    print(f"BACKUP-OK backup_id={outcome.manifest.backup_id} source={describe_url(database_url)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
