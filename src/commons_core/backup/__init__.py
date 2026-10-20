"""AIC backup & recovery — Stage 1 repository-side tooling (ADR-0009).

Scope (everything else stays designed-but-unimplemented — see
``docs/BACKUP.md`` for the Stage 1 vs future boundary):

* :func:`~commons_core.backup.pipeline.run_backup` — streaming
  ``pg_dump --format=custom`` piped through ``age`` public-key encryption
  with explicit process supervision: no plaintext dump file ever exists,
  failures of either subprocess delete the staged artifact and publish
  nothing (review correction 1).
* :mod:`~commons_core.backup.manifest` — the authoritative, non-sensitive
  backup manifest with documented consistency semantics; no row counts
  (correction 2).
* :mod:`~commons_core.backup.storage` — verified, atomic, conflict-safe
  publication with a commit marker as the ONLY success signal
  (correction 3), implemented over the local filesystem for Stage 1.
* :func:`~commons_core.backup.restore.run_restore_drill` — fail-closed
  synthetic restore into a disposable database with a content-blind
  application-level verification battery (correction-informed restore
  safety). There is intentionally NO production restore command.

Key hygiene (correction 6): this package never accepts private key
material; test identities are generated at runtime and scoped to temporary
directories; logs are scrubbed of credentials (see
:func:`~commons_core.backup.destination.scrub_secrets`).

Stage 2 adds :mod:`~commons_core.backup.recovery_keys` — offline recovery-key
custody packages (ADR-0010): two-copy redundancy, content-blind verification,
and exports for disaster recovery, with the same never-a-secret-in-argv rules.
"""

from .connection import (
    CanonicalConnection,
    ConnectionContractError,
    InProcessEnvironmentError,
    canonical_from_url,
)
from .destination import (
    UnsafeDestinationError,
    describe_url,
    scrub_secrets,
    validate_restore_destination,
)
from .manifest import BackupManifest, new_backup_id
from .pipeline import (
    ArtifactVerificationFailed,
    BackupError,
    BackupOutcome,
    DeadlineExceeded,
    DumpFailed,
    EncryptionFailed,
    PreflightError,
    PublicationFailed,
    run_backup,
)
from .recovery_keys import (
    AgeIdentity,
    PackageCreated,
    PackageExistsError,
    PackageFormatError,
    PackageVerificationError,
    RecoveryKeyError,
    UnsafeOutputError,
    VerificationReport,
    create_recovery_package,
    duplicate_recovery_package,
    export_age_identity,
    export_application_env,
    generate_age_identity,
    read_age_identity_file,
    unlock_recovery_package,
    verify_recovery_package,
)
from .restore import (
    DrillCleanupError,
    DrillFailure,
    DrillReport,
    run_restore_drill,
    verify_restored_database,
)
from .storage import (
    BackupConflictError,
    LocalFilesystemStorage,
    MissingBackupObject,
    PublicationUnsupportedError,
    PublishedSetCorrupted,
    StorageError,
)
from .tools import Toolbelt, ToolError, locate_toolbelt

__all__ = [
    # backup side
    "ArtifactVerificationFailed",
    "BackupConflictError",
    "BackupError",
    "BackupManifest",
    "BackupOutcome",
    "DeadlineExceeded",
    "DumpFailed",
    "EncryptionFailed",
    "LocalFilesystemStorage",
    "MissingBackupObject",
    "PreflightError",
    "PublicationFailed",
    "PublicationUnsupportedError",
    "PublishedSetCorrupted",
    "StorageError",
    "Toolbelt",
    "ToolError",
    "locate_toolbelt",
    "new_backup_id",
    "run_backup",
    # restore side
    "DrillCleanupError",
    "DrillFailure",
    "DrillReport",
    "run_restore_drill",
    "verify_restored_database",
    # safety helpers
    "CanonicalConnection",
    "ConnectionContractError",
    "InProcessEnvironmentError",
    "UnsafeDestinationError",
    "canonical_from_url",
    "describe_url",
    "scrub_secrets",
    "validate_restore_destination",
    # recovery custody (Stage 2)
    "AgeIdentity",
    "PackageCreated",
    "PackageExistsError",
    "PackageFormatError",
    "PackageVerificationError",
    "RecoveryKeyError",
    "UnsafeOutputError",
    "VerificationReport",
    "create_recovery_package",
    "duplicate_recovery_package",
    "export_age_identity",
    "export_application_env",
    "generate_age_identity",
    "read_age_identity_file",
    "unlock_recovery_package",
    "verify_recovery_package",
]
