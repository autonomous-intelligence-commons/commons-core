# Offline Preflight A v1.2 — Safety Design and Acceptance

**Status:** Functional acceptance recorded; further hardening and deployment review required.

## Goal

Before any deliberately controlled test-cluster startup, perform **read-only, fail-closed** checks on the intended Linux/WSL test environment and baseline PostgreSQL cluster. This preflight neither starts the database nor grants permission to modify it.

## Checked properties

- Expected user, WSL distribution, Linux filesystem and disabled Windows-drive automount/interop settings.
- No inherited PostgreSQL or backup-specific connection environment variables.
- One registered dedicated test cluster, **stopped and cleanly shut down**, using the expected PostgreSQL build and manual startup policy.
- Dedicated local listener/socket policy, PostgreSQL configuration paths, data directory ownership/mode, permitted include sources, duplicate/override settings, and narrowly specified authentication rules.
- Recorded cluster control-file identity and expected absence of local listeners in the offline state.

## Scoped results

User-observed WSL baseline run: **38 checks PASS, process exit 0**. The user separately observed rejection of injected client connection settings, wrong Linux user, and wrong expected cluster identity (each failed closed with exit 1). **16** isolated parsing/unit cases passed. Prior v1.1 rejected a standard optional package include directive and was superseded; that compatibility issue was addressed in v1.2 without changing the underlying PostgreSQL configuration.

## Limits

A script-based baseline is not a complete PostgreSQL configuration interpreter, an immutable deployment, a host-level network isolation proof, or a safeguard against post-check races. Hardcoded expected identity does not independently protect the script itself. Revalidate the actual live server in Online Preflight B, and later enforce checks inside a protected operation boundary.

**No destructive actions authorized.**
