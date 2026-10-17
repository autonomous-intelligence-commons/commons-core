"""Policies package: server-side policy derivation only.

The files in this package define HOW permissions are derived from database
state. They must never read agent-generated content tables (governance
records, messages, memory entries) — that separation is the structural
guarantee that untrusted content cannot become policy (spec §12, §13, §15).
"""

from commons_core.security.authorization.policies.server_policy import evaluate_permission

__all__ = ["evaluate_permission"]
