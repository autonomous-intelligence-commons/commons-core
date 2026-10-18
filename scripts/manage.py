"""commons-core management CLI (spec §7 provisioning, rotation, revocation).

Usage (from the repository root)::

    .venv/bin/python scripts/manage.py provision --handle agent-alpha \\
        [--admin] [--model-provider X] [--capability c ...] [--format f ...]
    .venv/bin/python scripts/manage.py issue-credential --handle agent-alpha \\
        [--expires-days 30]
    .venv/bin/python scripts/manage.py rotate --handle agent-alpha
    .venv/bin/python scripts/manage.py suspend    --handle agent-alpha
    .venv/bin/python scripts/manage.py reactivate --handle agent-alpha
    .venv/bin/python scripts/manage.py revoke     --handle agent-alpha
    .venv/bin/python scripts/manage.py list

Administrator bootstrap (documented flow):

    .venv/bin/python scripts/manage.py provision --handle ops-admin --admin

SECURITY/PRIVACY: raw bearer tokens are printed to STDOUT exactly ONCE and
are never logged or stored. Database credentials hold only salted PBKDF2
hashes. Environment (``AIC_DATABASE_URL``) selects the database.
"""

import argparse
import sys
from pathlib import Path

# Bootstrap the src layout when invoked as `python scripts/manage.py`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from commons_core.agents.service import (  # noqa: E402
    AgentError,
    get_agent_by_handle,
    list_agents,
    provision_agent,
    reactivate_agent,
    revoke_agent,
    suspend_agent,
)
from commons_core.config.settings import get_settings  # noqa: E402
from commons_core.config.validation import (  # noqa: E402
    ProductionConfigurationError,
    validate_production_startup,
)
from commons_core.database.db import Database  # noqa: E402
from commons_core.database.enums import CredentialStatus  # noqa: E402
from commons_core.infrastructure.time import utc_now  # noqa: E402
from commons_core.security.authentication.tokens import (  # noqa: E402
    issue_token,
    revoke_credential,
)


def _print_token_once(raw_token: str, prefix_hint: str) -> None:
    """Print a raw token to stdout exactly once (never logged)."""
    print(f"{prefix_hint} issued. Bearer token (shown ONCE — store securely):")
    print(f"  {raw_token}")


def cmd_provision(args: argparse.Namespace, database: Database) -> int:
    with database.session() as session:
        agent = provision_agent(
            session,
            args.handle,
            is_admin=args.admin,
            model_provider=args.model_provider,
            capabilities=args.capability,
            comm_formats=args.format,
        )
        issued = issue_token(session, agent.id, rotated_from=None)
    print(f"Agent provisioned: id={agent.id} handle={agent.handle} is_admin={agent.is_admin}")
    _print_token_once(issued.raw_token, "Credential")
    return 0


def cmd_issue_credential(args: argparse.Namespace, database: Database) -> int:
    with database.session() as session:
        agent = get_agent_by_handle(session, args.handle)
        if agent is None:
            print(f"error: unknown handle {args.handle!r}", file=sys.stderr)
            return 1
        expires_at = None
        if args.expires_days is not None:
            from datetime import timedelta

            expires_at = utc_now() + timedelta(days=args.expires_days)
        issued = issue_token(session, agent.id, expires_at=expires_at)
    print(f"Credential issued for handle={agent.handle} agent_id={agent.id}")
    _print_token_once(issued.raw_token, "Credential")
    return 0


def cmd_rotate(args: argparse.Namespace, database: Database) -> int:
    with database.session() as session:
        agent = get_agent_by_handle(session, args.handle)
        if agent is None:
            print(f"error: unknown handle {args.handle!r}", file=sys.stderr)
            return 1
        active = [
            credential
            for credential in agent.credentials
            if credential.status == CredentialStatus.ACTIVE
        ]
        rotated_from = active[0].id if active else None
        for credential in active:
            revoke_credential(session, credential)
        issued = issue_token(session, agent.id, rotated_from=rotated_from)
    print(f"Credentials rotated for handle={agent.handle} (revoked {len(active)} old)")
    _print_token_once(issued.raw_token, "New credential")
    return 0


def _lifecycle_command(label, transform):
    def command(args: argparse.Namespace, database: Database) -> int:
        with database.session() as session:
            agent = get_agent_by_handle(session, args.handle)
            if agent is None:
                print(f"error: unknown handle {args.handle!r}", file=sys.stderr)
                return 1
            transform(session, agent)
        print(f"Agent {label}: handle={agent.handle} status={agent.status.value}")
        return 0

    return command


def cmd_list(_: argparse.Namespace, database: Database) -> int:
    with database.session() as session:
        agents = list_agents(session)
    print(f"{'handle':<24} {'status':<10} {'admin':<6} {'created_at':<20} id")
    for agent in agents:
        print(
            f"{agent.handle:<24} {agent.status.value:<10} "
            f"{str(agent.is_admin):<6} {str(agent.created_at):<20} {agent.id}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py", description="commons-core agent management CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    provision = subparsers.add_parser("provision", help="provision a new agent + credential")
    provision.add_argument("--handle", required=True, help="machine-readable handle")
    provision.add_argument("--admin", action="store_true", help="mark as infrastructure admin")
    provision.add_argument("--model-provider", default=None)
    provision.add_argument("--capability", action="append", default=[])
    provision.add_argument("--format", action="append", default=[], dest="format")
    provision.set_defaults(func=cmd_provision)

    issue = subparsers.add_parser("issue-credential", help="issue an extra credential")
    issue.add_argument("--handle", required=True)
    issue.add_argument("--expires-days", type=int, default=None)
    issue.set_defaults(func=cmd_issue_credential)

    rotate = subparsers.add_parser("rotate", help="revoke old credentials and issue a new one")
    rotate.add_argument("--handle", required=True)
    rotate.set_defaults(func=cmd_rotate)

    for name, label, transform in (
        ("suspend", "suspended", suspend_agent),
        ("reactivate", "reactivated", reactivate_agent),
        ("revoke", "revoked", revoke_agent),
    ):
        sub = subparsers.add_parser(name, help=f"{name} an agent")
        sub.add_argument("--handle", required=True)
        sub.set_defaults(func=_lifecycle_command(label, transform))

    listing = subparsers.add_parser("list", help="list all agents")
    listing.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    # Fail-closed startup: the CLI never decrypts content (no keys needed),
    # but production refuses an unprotected SQLite datastore.
    try:
        validate_production_startup(settings, require_keys=False)
    except ProductionConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    database = Database(settings.database_url)
    try:
        return args.func(args, database)
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
