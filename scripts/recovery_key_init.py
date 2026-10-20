"""Create (or duplicate) an AIC recovery-key custody package.

Owner-side entry point of Backup & Recovery Stage 2 (ADR-0010). It
packages — never prints — secret material: age identities and application
master keys are sealed into an age-encrypted secret section; only public
recipients and key ids reach stdout.

The default mode generates a fresh custody identity; the operator moves
the generated identity file to independent offline storage immediately
(docs/RECOVERY_KEYS.md). All material in tests is SYNTHETIC; generating
real production keys is an explicit human-authorized action performed
offline by the owner.

Exit codes: 0 = package created/duplicated; 1 = operational failure;
2 = configuration/usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commons_core.backup.recovery_keys import (  # noqa: E402
    PackageExistsError,
    RecoveryKeyError,
    UnsafeOutputError,
    create_recovery_package,
    duplicate_recovery_package,
    read_age_identity_file,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recovery_key_init.py",
        description="Create or duplicate an AIC recovery-key custody package.",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="fresh, empty, OFFLINE output directory (refused inside repos/sync folders)",
    )
    parser.add_argument(
        "--duplicate-from",
        help="existing package directory to copy into --out-dir (redundancy)",
    )
    parser.add_argument(
        "--age-identity-file",
        action="append",
        default=[],
        help="existing age-keygen identity file to package (repeatable)",
    )
    parser.add_argument(
        "--generate-age-identity",
        action="store_true",
        help="generate a fresh operational age identity to package",
    )
    parser.add_argument(
        "--with-application-keys",
        action="store_true",
        help="read AIC_MASTER_KEYS / AIC_ACTIVE_KEY_ID from the environment",
    )
    parser.add_argument(
        "--backup-domain-only",
        action="store_true",
        help="package age identities without application master keys",
    )
    parser.add_argument(
        "--custody-identity-file",
        action="append",
        default=[],
        help="existing custody identity encrypting the secret section (repeatable)",
    )
    parser.add_argument(
        "--custody-recipient",
        action="append",
        default=[],
        help="extra PUBLIC custody recipient (age1…; its identity is held elsewhere)",
    )
    parser.add_argument(
        "--no-generate-custody-identity",
        action="store_true",
        help="do not auto-generate a custody identity (use supplied ones only)",
    )
    parser.add_argument("--app-commit", default=None, help="application release/commit id")
    parser.add_argument("--alembic-revision", default=None, help="database schema revision")
    parser.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    return parser


def _config_error(message: str) -> int:
    sys.stderr.write(f"recovery_key_init: refusing to run — {message} (fail-closed)\n")
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.duplicate_from:
        creation_flags = any(
            [
                args.age_identity_file,
                args.generate_age_identity,
                args.with_application_keys,
                args.backup_domain_only,
                args.custody_identity_file,
                args.custody_recipient,
            ]
        )
        if creation_flags:
            return _config_error("--duplicate-from cannot be combined with creation options")
        try:
            target = duplicate_recovery_package(args.duplicate_from, args.out_dir)
        except (UnsafeOutputError, PackageExistsError) as exc:
            return _config_error(str(exc))
        except RecoveryKeyError as exc:
            sys.stderr.write(f"recovery_key_init: duplicate failed — {exc}\n")
            return 1
        sys.stdout.write(f"RECOVERY-DUPLICATE-OK dir={target}\n")
        sys.stdout.write(
            "verify both copies now:\n"
            f"  python scripts/recovery_key_verify.py --package-dir {target} "
            f"--compare-dir {args.duplicate_from}\n"
        )
        return 0

    identities = []
    for path in args.age_identity_file:
        try:
            identities.append(read_age_identity_file(path))
        except (RecoveryKeyError, OSError) as exc:
            return _config_error(f"cannot read age identity file {path}: {exc}")
    if not identities and not args.generate_age_identity:
        return _config_error(
            "no age identity source — pass --generate-age-identity or --age-identity-file PATH"
        )

    master_keys = None
    active_key_id = None
    if args.with_application_keys:
        master_keys = os.environ.get("AIC_MASTER_KEYS", "").strip()
        active_key_id = os.environ.get("AIC_ACTIVE_KEY_ID", "").strip()
        if not master_keys or not active_key_id:
            return _config_error(
                "--with-application-keys requires AIC_MASTER_KEYS and AIC_ACTIVE_KEY_ID "
                "in the environment (secrets are never accepted on the command line)"
            )
    elif not args.backup_domain_only:
        return _config_error(
            "choose an application-domain mode: --with-application-keys or --backup-domain-only"
        )

    custody = []
    for path in args.custody_identity_file:
        try:
            custody.append(read_age_identity_file(path, label=f"custody-{len(custody) + 1}"))
        except (RecoveryKeyError, OSError) as exc:
            return _config_error(f"cannot read custody identity file {path}: {exc}")

    try:
        created = create_recovery_package(
            args.out_dir,
            age_identities=identities or None,
            generate_operational_identity=args.generate_age_identity,
            custody_identities=custody or None,
            custody_recipients_only=args.custody_recipient or None,
            generate_custody_identity=not args.no_generate_custody_identity,
            master_keys=master_keys,
            active_key_id=active_key_id,
            app_commit=args.app_commit,
            alembic_revision=args.alembic_revision,
        )
    except (UnsafeOutputError, PackageExistsError) as exc:
        return _config_error(str(exc))
    except RecoveryKeyError as exc:
        sys.stderr.write(f"recovery_key_init: package creation failed — {exc}\n")
        return 1

    sys.stdout.write(
        f"RECOVERY-PACKAGE-OK generation={created.generation} dir={created.package_dir}\n"
    )
    sys.stdout.write(f"custody_manifest_sha256={created.custody_manifest_sha256}\n")
    sys.stdout.write("  ^ record this in at least two INDEPENDENT places (runbook)\n")
    if created.custody_identity_path is not None:
        sys.stdout.write(
            f"CUSTODY-IDENTITY-FILE {created.custody_identity_path}\n"
            "  ^ MOVE this file to independent offline storage NOW; it is the key to\n"
            "    this package and must never travel with the package copies\n"
        )
    for recipient in created.packaged_recipients:
        sys.stdout.write(f"packaged-recipient (public, safe) {recipient}\n")
    if created.application_key_ids:
        sys.stdout.write(
            f"application-key-ids {','.join(created.application_key_ids)} "
            f"active={created.active_key_id}\n"
        )
    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "generation": created.generation,
                    "package_dir": str(created.package_dir),
                    "custody_manifest_sha256": created.custody_manifest_sha256,
                    "custody_identity_path": (
                        str(created.custody_identity_path)
                        if created.custody_identity_path
                        else None
                    ),
                    "custody_recipients": list(created.custody_recipients),
                    "packaged_recipients": list(created.packaged_recipients),
                    "application_key_ids": list(created.application_key_ids),
                    "active_key_id": created.active_key_id,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
