"""PRIVACY-PRESERVING LOGGING (spec §11, §20).

PRIVACY RULE — enforced across every log statement in this codebase:

- NEVER log payload, message, memory or governance content.
- NEVER log credentials (raw tokens, hashes, salts) or encryption keys.
- Log categories, agent handles/ids, counts and limits only.

Implementation: stdlib logging to stderr with the format
``%(asctime)s %(levelname)s %(name)s %(message)s``; level comes from
``AIC_LOG_LEVEL`` (default INFO). Configuration is idempotent so repeated
``create_app()`` calls (tests) do not duplicate handlers. The
``commons_core`` logger propagates so test tooling (caplog) can inspect
records.

Operational-error policy: unexpected exceptions are logged with their CLASS
NAME and request context only — not full tracebacks or driver messages,
which could embed content values in error text.
"""

import logging
import sys

from commons_core.config.settings import Settings

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_HANDLER_ATTR = "_commons_core_handler"


def configure_logging(settings: Settings) -> None:
    """Configure the ``commons_core`` logger tree (idempotent)."""
    logger = logging.getLogger("commons_core")
    if not any(getattr(handler, _HANDLER_ATTR, False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        setattr(handler, _HANDLER_ATTR, True)
        logger.addHandler(handler)
    level_name = settings.log_level.upper()
    logger.setLevel(level_name)
    logger.propagate = True
