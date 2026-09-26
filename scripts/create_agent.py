"""Create an agent from a v4 agent_config JSON file, without going through the UI.

Runs the same checks as POST /api/agents/ (schema 'agent_config' v4, skill
references) and inserts the row for the account's org. The agent is private
to its owner, as the API would create it.

Usage (from the repo root, inside the dev container):
    python -m scripts.create_agent --email you@example.com \
        --name "Logistics Analyst" \
        --config READMEs/runner/logistics_analyst_agent.pedestal.json

Against production:
    python -m scripts.create_agent --env-file .env.production --email you@example.com \
        --name "Logistics Analyst" \
        --config READMEs/runner/logistics_analyst_agent.pedestal.json

Add --dry-run to validate and show the resolved account/org without writing.
"""
import argparse
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--account-id", type=int, help="Owner account")
    who.add_argument("--email", help="Owner account's email (looked up in the target database)")
    parser.add_argument("--name", required=True, help="Agent name")
    parser.add_argument("--config", required=True, help="Path to a v4 agent_config JSON file")
    parser.add_argument("--org-id", type=int, help="Org to create the agent in (default: the account's default org)")
    parser.add_argument("--env-file", default=".env",
                        help="Env file with POSTGRES_URL (default .env; use .env.production for prod)")
    parser.add_argument("--dry-run", action="store_true", help="Validate and resolve, but do not insert")
    args = parser.parse_args()

    # override=True: the container's own env may already carry a POSTGRES_URL
    # for another environment; the file named here must win.
    load_dotenv(args.env_file, override=True)

    from fastapi import HTTPException
    from src.db.database import SessionLocal
    from src.db.models import Account, Agent, OrganizationMember
    from src.routers.agents.skill_refs import validate_skill_refs
    from src.schemas import validate_against_schema

    name = args.name.strip()
    if not name:
        sys.exit("agent name cannot be empty")

    with open(args.config) as f:
        config = json.load(f)
    if config.get("version") != 4:
        sys.exit("only config version 4 is supported")
    validate_against_schema(config, "agent_config", 4)

    db = SessionLocal()
    try:
        if args.account_id is not None:
            account = db.query(Account).filter(Account.id == args.account_id).first()
        else:
            account = db.query(Account).filter(Account.email.ilike(args.email)).first()
        if not account:
            sys.exit(f"no account {args.account_id or args.email} in this database")

        org_id = args.org_id or account.default_org_id
        if org_id is None:
            sys.exit(f"account {account.id} has no default org; pass --org-id")
        is_member = db.query(OrganizationMember).filter(
            OrganizationMember.account_id == account.id,
            OrganizationMember.org_id == org_id,
        ).first()
        if not is_member:
            sys.exit(f"account {account.id} is not a member of org {org_id}")
        print(f"account {account.email} -> id {account.id}, org {org_id}")

        try:
            validate_skill_refs(db, config, SimpleNamespace(org_id=org_id, account_id=account.id))
        except HTTPException as e:
            sys.exit(f"skill reference check failed: {e.detail}")

        existing = db.query(Agent).filter(
            Agent.org_id == org_id, Agent.account_id == account.id, Agent.name == name,
        ).first()
        if existing:
            sys.exit(f"agent {name!r} already exists for this account (id {existing.id}); "
                     "update it through the API instead")

        if args.dry_run:
            print("dry run: config valid, nothing written")
            return

        agent = Agent(org_id=org_id, account_id=account.id, name=name, config=config)
        db.add(agent)
        db.commit()
        db.refresh(agent)
        print(f"created agent {agent.id}: {agent.name}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
