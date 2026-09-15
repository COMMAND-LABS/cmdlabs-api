import logging
import os
import boto3

from src.services import ses_logging

logger = logging.getLogger(__name__)

EMAIL_KIND = "reset_password_link"
# NOTE: kalygo.io, not cmdlabs.io like the other senders. See the same note in
# send_password_has_been_reset_email_ses.
FROM_ADDRESS = "noreply@kalygo.io"


def send_reset_password_link_email_ses(account_id: int, to_email: str, reset_token: str):
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
                    'Data': 'Password Reset Request'
                },
                'Body': {
                    'Html': {
                        'Data': f"<p>Reset Token: {reset_token}</p><a href='{os.getenv('API_HOSTNAME')}/reset-password?account-id={account_id}'>Reset Password</a>"
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
