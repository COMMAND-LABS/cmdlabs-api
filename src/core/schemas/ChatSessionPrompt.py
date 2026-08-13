
from pydantic import BaseModel


class ChatSessionPrompt(BaseModel):
    prompt: str
    sessionId: str
    # Optional PDF attachment (base64 encoded)
    pdf: str | None = None
    pdfFilename: str | None = None
    # PDF processing mode:
    # - True: Use vision (images) - for scanned PDFs, charts, visual layout
    # - False: Use text extraction - for data extraction, cheaper with gpt-4o-mini
    pdfUseVision: bool | None = False

    # Optional image attachment (base64 encoded) for vision models.
    image: str | None = None
    # Optional inline text content for txt/csv/md attachments.
    documentText: str | None = None

    # GCS reference for the persisted attachment (returned by ai-api
    # POST /api/files/upload). Stored on the chat message so the original file
    # can be resolved later. The model-facing content still rides inline above.
    gcsBucket: str | None = None
    gcsFilePath: str | None = None
    attachmentFilename: str | None = None
    attachmentContentType: str | None = None
