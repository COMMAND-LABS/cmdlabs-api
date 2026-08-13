"""Request/response schemas for the one-shot PDF -> FAQ generation endpoint."""

from pydantic import BaseModel, Field


class ModelSelection(BaseModel):
    """User-selected model. `provider` must match the create_llm factory keys
    ("openai" | "anthropic" | "google" | "kimi" | "ollama")."""

    provider: str
    model: str


class PdfToFaqRequest(BaseModel):
    pdf: str  # base64-encoded PDF
    pdfFilename: str | None = None
    model: ModelSelection
    # Text extraction (False) is cheaper and better for Q&A extraction; vision
    # (True) is the fallback for scanned/image-only PDFs.
    pdfUseVision: bool | None = False


class FaqPair(BaseModel):
    question: str = Field(description="A concise, self-contained question answerable from the document.")
    answer: str = Field(description="The answer, grounded strictly in the document content.")


class FaqList(BaseModel):
    """Structured-output target handed to llm.with_structured_output()."""

    pairs: list[FaqPair] = Field(description="The list of question/answer pairs.")
