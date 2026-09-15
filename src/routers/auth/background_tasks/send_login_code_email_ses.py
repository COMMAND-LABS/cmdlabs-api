import logging
import os
import boto3

from src.services import ses_logging

logger = logging.getLogger(__name__)

EMAIL_KIND = "login_code"
FROM_ADDRESS = "noreply@cmdlabs.io"


def send_login_code_email_ses(to_email: str, code: str) -> None:
    ses_logging.log_attempt(logger, kind=EMAIL_KIND, to_email=to_email,
                            source=FROM_ADDRESS)
    try:
        client = boto3.client(
            "ses",
            region_name=os.getenv("AWS_REGION"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_KEY"),
        )

        response = client.send_email(
            Source=FROM_ADDRESS,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": "Your COMMAND LABS sign-in code"},
                "Body": {
                    "Html": {
                        "Data": (
                            f"<p>Your sign-in code is:</p>"
                            f"<h2 style='letter-spacing:0.2em'>{code}</h2>"
                            f"<p>This code expires in 10 minutes. "
                            f"If you did not request this, you can safely ignore this email.</p>"
                        )
                    },
                    "Text": {
                        "Data": (
                            f"Your COMMAND LABS sign-in code is: {code}\n\n"
                            f"This code expires in 10 minutes.\n"
                            f"If you did not request this, you can safely ignore this email."
                        )
                    },
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 — best effort; the caller must not fail
        ses_logging.log_failed(logger, kind=EMAIL_KIND, to_email=to_email,
                               source=FROM_ADDRESS, exc=exc)
        return

    ses_logging.log_accepted(logger, kind=EMAIL_KIND, to_email=to_email,
                             source=FROM_ADDRESS, response=response)
