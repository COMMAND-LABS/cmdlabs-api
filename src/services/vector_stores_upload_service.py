import logging
import os
import uuid
import json
from datetime import datetime
from typing import Dict, Any, Optional
from fastapi import UploadFile
from sqlalchemy.orm import Session
from src.services import account_gcs_service
from src.clients.pubsub_client import PubSubClient

logger = logging.getLogger(__name__)

# Each ingest cloud function has its OWN topic and its own subscription. A
# message on the wrong topic is not an error anywhere: the publish succeeds,
# this service returns success, the UI says "queued", and the file is simply
# never ingested — the VectorDbIngestionLog row sits at PENDING forever. So the
# topic is chosen by the CALLER, which is the only place that knows what kind of
# upload this is, rather than being inferred here from the filename.
#
# qna-ingest-topic -> two-column Q&A CSVs and reviewed PDF-to-FAQ pairs; builds
#                     one vector per Q&A pair.
# txt-ingest-topic -> kalygo3-txt-ingest-cloud-function-python; chunks .txt/.md
#                     free text and parses YAML front matter into file_* keys.
QNA_INGEST_TOPIC = "qna-ingest-topic"
TXT_INGEST_TOPIC = "txt-ingest-topic"


class VectorStoresUploadService:
    """
    Upload service for the Vector Stores module.

    Uploads files to the account's OWN Google Cloud Storage bucket (per-account
    credentials) and publishes a message to an ingest Pub/Sub topic for async
    ingestion. The Pub/Sub message carries account_id so the ingest cloud
    function can resolve the same per-account credentials to download.

    Which topic is a per-call decision — see the module constants above and the
    `topic_name` argument to upload_file_and_publish.
    """

    def __init__(self):
        self.project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "kalygo-436411")

    async def upload_file_and_publish(
        self,
        file: UploadFile,
        user_id: str,
        user_email: str,
        index_name: str,
        namespace: str,
        jwt: str,
        db: Session,
        account_id: int,
        topic_name: str = QNA_INGEST_TOPIC,
        batch_number: Optional[str] = None,
        comment: Optional[str] = None,
        extra_message_fields: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Upload file to the account's GCS bucket and publish a message to Pub/Sub.

        `topic_name` selects the ingest cloud function. It defaults to the Q&A
        topic because that is what the CSV and PDF-to-FAQ flows use; anything
        that is not Q&A-shaped must pass it explicitly.

        `extra_message_fields` are merged into the published Pub/Sub message. The
        PDF-to-FAQ flow uses this to carry the reviewed Q&A pairs (so the ingest
        cloud function builds vectors from them) alongside the original PDF that
        is the stored/referenced source.

        Raises account_gcs_service.AccountGcsCredentialMissing when the account
        has no GCS credential configured (callers map this to HTTP 400).
        """
        file_content = await file.read()
        return await self.upload_bytes_and_publish(
            file_bytes=file_content,
            filename=file.filename,
            content_type=file.content_type,
            user_id=user_id,
            user_email=user_email,
            index_name=index_name,
            namespace=namespace,
            jwt=jwt,
            db=db,
            account_id=account_id,
            topic_name=topic_name,
            batch_number=batch_number,
            comment=comment,
            extra_message_fields=extra_message_fields,
        )

    async def upload_bytes_and_publish(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        content_type: Optional[str],
        user_id: str,
        user_email: str,
        index_name: str,
        namespace: str,
        jwt: Optional[str],
        db: Session,
        account_id: int,
        topic_name: str = QNA_INGEST_TOPIC,
        batch_number: Optional[str] = None,
        comment: Optional[str] = None,
        extra_message_fields: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        The bytes form of upload_file_and_publish, for callers that build the
        file themselves (the knowledgeWrite approval renders a markdown note).
        Same storage path, same message, same return shape.
        """
        try:
            file_id = str(uuid.uuid4())
            timestamp = datetime.now().isoformat()

            if batch_number is None:
                batch_number = str(uuid.uuid4())

            gcs_file_path = f"vector_stores/{index_name}/{namespace}/{batch_number}/{file_id}/{filename}"

            file_content = file_bytes

            # Store in the bucket bound to this knowledge base (falls back to the
            # owner's default GCS credential when the index has no explicit bind).
            ref = account_gcs_service.upload_bytes_for_index(
                db,
                account_id,
                index_name,
                file_bytes=file_content,
                gcs_file_path=gcs_file_path,
                content_type=content_type,
            )
            gcs_bucket = ref["gcs_bucket"]

            logger.info("File uploaded to GCS: gs://%s/%s", gcs_bucket, gcs_file_path)

            message_data = {
                "file_id": file_id,
                "filename": filename,
                "gcs_bucket": gcs_bucket,
                "gcs_file_path": gcs_file_path,
                "file_size": len(file_content),
                "content_type": content_type,
                "user_id": user_id,
                "user_email": user_email,
                "account_id": account_id,
                "index_name": index_name,
                "namespace": namespace,
                "batch_number": batch_number,
                "upload_timestamp": timestamp,
                "processing_status": "pending",
                "jwt": jwt,
                "module": "vector_stores",
                "comment": comment
            }

            if extra_message_fields:
                message_data.update(extra_message_fields)

            publisher_client = PubSubClient.get_publisher_client()
            topic_path = publisher_client.topic_path(self.project_id, topic_name)

            message_json = json.dumps(message_data)
            message_bytes = message_json.encode("utf-8")

            future = publisher_client.publish(topic_path, data=message_bytes)
            message_id = future.result()

            # Topic included: "published successfully" to the wrong topic looks
            # identical to the right one in every other log line.
            logger.info(
                "Published message %s for file %s to topic %s",
                message_id, filename, topic_name
            )
            
            return {
                "success": True,
                "file_id": file_id,
                "filename": filename,
                "gcs_bucket": gcs_bucket,
                "gcs_file_path": gcs_file_path,
                "message_id": message_id,
                "batch_number": batch_number,
                "index_name": index_name,
                "namespace": namespace,
                "processing_status": "pending",
                "message": "File uploaded successfully and queued for vector database ingestion",
                "pubsub_topic": topic_name,
                "module": "vector_stores"
            }

        except account_gcs_service.AccountGcsCredentialMissing:
            # Propagate so the router can return a clear HTTP 400 prompting the
            # account to configure GCS credentials.
            raise
        except Exception:
            logger.exception("Error in VectorStoresUploadService")
            return {
                "success": False,
                "error": "Failed to upload file. Please try again.",
                "module": "vector_stores"
            }
