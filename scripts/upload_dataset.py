"""Upload a local file into an account's own GCS bucket, for dataset-backed tools.

The timeSeriesForecast and codeExecution tools read datasets from the agent
owner's bucket (the one bound to the owner's default GCS credential) at the
`gcsPath` named in the tool config. This puts a file there.

Usage (from the repo root, inside the dev container):
    python -m scripts.upload_dataset --account-id 1 \
        --file data/mock/duty_spend.csv --gcs-path datasets/duty_spend.csv
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.db.database import SessionLocal
from src.services import account_gcs_service


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account-id", type=int, required=True, help="Owner account whose bucket receives the file")
    parser.add_argument("--file", required=True, help="Local file to upload")
    parser.add_argument("--gcs-path", required=True, help="Object path inside the bucket, e.g. datasets/duty_spend.csv")
    parser.add_argument("--content-type", default="text/csv")
    args = parser.parse_args()

    with open(args.file, "rb") as f:
        payload = f.read()

    db = SessionLocal()
    try:
        ref = account_gcs_service.upload_bytes(
            db, args.account_id,
            file_bytes=payload, gcs_file_path=args.gcs_path, content_type=args.content_type,
        )
    finally:
        db.close()
    print(f"uploaded {len(payload)} bytes to gs://{ref['gcs_bucket']}/{ref['gcs_file_path']}")


if __name__ == "__main__":
    main()
