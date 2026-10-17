"""Activity and collaboration infrastructure module (Agent C — Phase 1, spec §14).

Reserved for the activity domain built on the foundation layer: activity
lifecycle, membership states, associated channels.

Contracts this module MUST use:
- ``EnvelopeCipher.seal/open`` for encrypted summaries.
- ``require_permission(activity:manage:{id})`` / ``activity:participate``.
- Models: Activity, ActivityMember.
"""
