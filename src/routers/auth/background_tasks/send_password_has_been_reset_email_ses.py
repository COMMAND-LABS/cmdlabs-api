import logging
import os
import boto3

from src.services import ses_logging

logger = logging.getLogger(__name__)

EMAIL_KIND = "password_has_been_reset"
# NOTE: kalygo.io, not cmdlabs.io like the other senders. If this identity is no
# longer verified in SES the send fails outright — the `failed` log line names
# the code (MailFromDomainNotVerified / MessageRejected) when that is the case.
FROM_ADDRESS = "noreply@kalygo.io"


def send_password_has_been_reset_email_ses(to_email: str):
    ses_logging.log_attempt(logger, kind=EMAIL_KIND, to_email=to_email,
                            source=FROM_ADDRESS)
    try:
        client = boto3.client(
            'ses',
            region_name=os.getenv("AWS_REGION"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_KEY")
        )

        response = client.send_email(
            Source=FROM_ADDRESS,
            Destination={
                'ToAddresses': [to_email]
            },
            Message={
                'Subject': {
                    'Data': 'Password has been reset'
                },
                'Body': {
                    'Html': {
                        'Data': f"<p>Your password has been reset</p>"
                    }
                }
            }
        )
    except Exception as exc:  # noqa: BLE001 — best effort; the caller must not fail
        ses_logging.log_failed(logger, kind=EMAIL_KIND, to_email=to_email,
                               source=FROM_ADDRESS, exc=exc)
        return

    ses_logging.log_accepted(logger, kind=EMAIL_KIND, to_email=to_email,
                             source=FROM_ADDRESS, response=response)
