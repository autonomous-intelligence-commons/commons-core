"""Verify (and optionally unlock) an AIC recovery-key custody package.

Content-blind verification: PASS/FAIL per check, no secret material on
stdout ever. Exit codes: 0 = all requested checks passed; 1 = at least one
check failed; 2 = configuration/usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commons_core.backup.recovery_keys import (  # noqa: E402
    PackageExistsError,
    RecoveryKeyError,
    export_age_identity,
    export_application_env,
    unlock_recovery_package,
    verify_recovery_package,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recovery_key_verify.py",
        description="Verify an AIC recovery-key custody package (content-blind).",
    )
    parser.add_argument("--package-dir", required=True, help="package directory to verify")
    parser.add_argument(
        "--expected-sha256",
        help="independently recorded custody_manifest_sha256 (authenticity check)",
    )
    parser.add_argument("--compare-dir", help="second package copy for byte-identity check")
    parser.add_argument(
        "--identity-file",
        help="custody identity file (enables decryption checks and exports)",
    )
    parser.add_argument(
        "--check-decrypt",
        action="store_true",
        help="decrypt the secret section and verify payload claims",
    )
    parser.add_argument(
        "--rotation-check",
        help="backup manifest JSON whose recipient coverage should be checked",
    )
    parser.add_argument(
        "--export-identity",
        action="append",
        default=[],
        metavar="LABEL:PATH",
        help="export a packaged age identity to PATH (repeatable)",
    )
    parser.add_argument(
        "--export-env-file",
        help="write AIC_MASTER_KEYS / AIC_ACTIVE_KEY_ID to an env file",
    )
    parser.add_argument("--report-json", help="write the verification report as JSON")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    return parser


def _config_error(message: str) -> int:
    sys.stderr.write(f"recovery_key_verify: refusing to run — {message} (fail-closed)\n")
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    wants_exports = bool(args.export_identity or args.export_env_file)
    if args.check_decrypt and not args.identity_file:
        return _config_error("--check-decrypt requires --identity-file")
    if wants_exports and not args.identity_file:
        return _config_error("exports require --identity-file (decryption is needed)")

    try:
        report = verify_recovery_package(
            args.package_dir,
            expected_sha256=args.expected_sha256,
            compare_dir=args.compare_dir,
            identity_file=args.identity_file,
            check_decrypt=args.check_decrypt or wants_exports,
            rotation_manifest_path=args.rotation_check,
        )
    except RecoveryKeyError as exc:
        sys.stderr.write(f"recovery_key_verify: {exc}\n")
        return 1

    if wants_exports and args.identity_file and report.passed:
        try:
            payload, _ = unlock_recovery_package(args.package_dir, args.identity_file)
            for spec in args.export_identity:
                label, sep, path = spec.partition(":")
                if not sep or not label.strip() or not path.strip():
                    return _config_error(f"--export-identity expects LABEL:PATH (got {spec!r})")
                export_age_identity(payload, label.strip(), path.strip())
                sys.stdout.write(f"EXPORT-IDENTITY {label.strip()} -> {path.strip()}\n")
            if args.export_env_file:
                export_application_env(payload, args.export_env_file)
                sys.stdout.write(f"EXPORT-ENV -> {args.export_env_file}\n")
        except PackageExistsError as exc:
            return _config_error(str(exc))
        except RecoveryKeyError as exc:
            sys.stderr.write(f"recovery_key_verify: export failed — {exc}\n")
            return 1

    sys.stdout.write(report.render() + "\n")
    if args.json:
        sys.stdout.write(report.to_json() + "\n")
    if args.report_json:
        Path(args.report_json).write_text(report.to_json() + "\n", encoding="utf-8")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
