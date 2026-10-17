"""Durable scheduling and worker module (Agent C — Phase 1, spec §16).

Reserved for the task scheduling domain: leases, retries, idempotency,
timeout handling, emergency-stop-aware worker loop.

Contracts this module MUST use:
- Models: ScheduledTask (payload column is for NON-SENSITIVE control data only).
- ``record_event`` for operational security events.
- SystemState (key ``emergency_stop``) MUST be honored by the worker loop.
"""
