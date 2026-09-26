"""Upload a local file into an account's own GCS bucket, for dataset-backed tools.

The timeSeriesForecast and codeExecution tools read datasets from the agent
owner's bucket (the one bound to the owner's default GCS credential) at the
`gcsPath` named in the tool config. This puts a file there.

Usage (from the repo root, inside the dev container):
    python -m scripts.upload_dataset --email you@example.com \
        --file data/mock/duty_spend.csv --gcs-path datasets/duty_spend.csv

Against production (the credential lives in the production database):
    python -m scripts.upload_dataset --env-file .env.production --email you@example.com \
        --file data/mock/duty_spend.csv --gcs-path datasets/duty_spend.csv
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--account-id", type=int, help="Owner account whose bucket receives the file")
    who.add_argument("--email", help="Owner account's email (looked up in the target database)")
    parser.add_argument("--file", required=True, help="Local file to upload")
    parser.add_argument("--gcs-path", required=True, help="Object path inside the bucket, e.g. datasets/duty_spend.csv")
    parser.add_argument("--content-type", default="text/csv")
    parser.add_argument("--env-file", default=".env",
                        help="Env file with POSTGRES_URL and CREDENTIALS_ENCRYPTION_KEY (default .env; use .env.production for prod)")
    args = parser.parse_args()

    # override=True: the container's own env may already carry a POSTGRES_URL
    # for another environment; the file named here must win.
    load_dotenv(args.env_file, override=True)

    from src.db.database import SessionLocal
    from src.db.models import Account
    from src.services import account_gcs_service

    with open(args.file, "rb") as f:
        payload = f.read()

    db = SessionLocal()
    try:
        account_id = args.account_id
        if account_id is None:
            account = db.query(Account).filter(Account.email.ilike(args.email)).first()
            if not account:
                sys.exit(f"no account with email {args.email} in this database")
            account_id = account.id
            print(f"account {args.email} -> id {account_id}")
        ref = account_gcs_service.upload_bytes(
            db, account_id,
            file_bytes=payload, gcs_file_path=args.gcs_path, content_type=args.content_type,
        )
    finally:
        db.close()
    print(f"uploaded {len(payload)} bytes to gs://{ref['gcs_bucket']}/{ref['gcs_file_path']}")


if __name__ == "__main__":
    main()
