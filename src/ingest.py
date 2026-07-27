"""Turn an uploaded file into content blocks the Messages API accepts.

PDFs and images go to Claude natively rather than through a local OCR step —
that keeps scanned and photographed documents on the same path as digital
ones, which is the realistic case for a clinical intake pile. pdfplumber is
used only to show the reviewer what text layer (if any) the PDF carries.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

TEXT_SUFFIXES = {".txt", ".md", ".rtf", ".csv"}

# Native PDF support caps out well below this, but a 2-day PoC has no business
# uploading a 600-page chart either.
MAX_PDF_PAGES = 100


class UnsupportedDocument(Exception):
    """Raised when a file type has no ingestion path."""


@dataclass
class IngestedDocument:
    """A document normalized into API content blocks plus a human preview."""

    filename: str
    kind: str  # "text" | "pdf" | "image"
    blocks: list[dict]
    preview: str
    note: str = ""


def _b64(data: bytes) -> str:
    # No newlines: the API rejects wrapped base64 in document/image sources.
    return base64.standard_b64encode(data).decode("utf-8")


def _pdf_text_layer(data: bytes) -> tuple[str, str]:
    """Best-effort text preview. Returns (preview, note)."""
    try:
        import pdfplumber
    except ImportError:
        return "", "pdfplumber not installed - preview unavailable."

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages[:MAX_PDF_PAGES]
            text = "\n\n".join((page.extract_text() or "") for page in pages).strip()
    except Exception as exc:  # a malformed PDF should not block extraction
        return "", f"Could not read text layer ({exc.__class__.__name__}). Sending pages as images."

    if not text:
        return "", "No text layer found - this looks scanned. Claude will read it visually."
    return text, ""


def ingest_bytes(data: bytes, filename: str) -> IngestedDocument:
    """Normalize raw file bytes into Messages API content blocks."""
    suffix = Path(filename).suffix.lower()

    if suffix in TEXT_SUFFIXES:
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            raise UnsupportedDocument(f"{filename} is empty.")
        return IngestedDocument(
            filename=filename,
            kind="text",
            blocks=[{"type": "text", "text": text}],
            preview=text,
        )

    if suffix == ".pdf":
        preview, note = _pdf_text_layer(data)
        return IngestedDocument(
            filename=filename,
            kind="pdf",
            blocks=[
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": _b64(data),
                    },
                }
            ],
            preview=preview,
            note=note,
        )

    if suffix in IMAGE_MEDIA_TYPES:
        return IngestedDocument(
            filename=filename,
            kind="image",
            blocks=[
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": IMAGE_MEDIA_TYPES[suffix],
                        "data": _b64(data),
                    },
                }
            ],
            preview="",
            note="Image input - Claude reads this visually.",
        )

    supported = sorted(TEXT_SUFFIXES | {".pdf"} | set(IMAGE_MEDIA_TYPES))
    raise UnsupportedDocument(f"Unsupported file type '{suffix}'. Supported: {', '.join(supported)}")


def ingest_path(path: str | Path) -> IngestedDocument:
    path = Path(path)
    return ingest_bytes(path.read_bytes(), path.name)
