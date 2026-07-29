"""Turn an uploaded file into a provider-neutral payload.

PDFs and images are handed to the model as raw bytes rather than through a
local OCR step — that keeps scanned and photographed documents on the same
path as digital ones, which is the realistic case for a clinical intake pile.
pdfplumber is used only to show the reviewer what text layer (if any) the PDF
carries.

This module deliberately knows nothing about any particular vendor's request
format. It yields bytes plus a MIME type; `extract.py` owns the translation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

# Mirrors the SDK's accepted image MIME types. TIFF earns its place here:
# scanned clinical documents and inbound faxes are routinely TIFF.
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

TEXT_SUFFIXES = {".txt", ".md", ".rtf", ".csv"}

# Gemini accepts PDFs up to 1000 pages, but a 2-day PoC has no business
# uploading a 600-page chart either. This bound is on the text preview only.
MAX_PDF_PAGES = 100

# Inline base64 caps the whole request at 20 MB. Past that a real deployment
# would upload via the Files API first; the PoC just declines.
MAX_INLINE_BYTES = 20 * 1024 * 1024


class UnsupportedDocument(Exception):
    """Raised when a file type has no ingestion path."""


@dataclass
class IngestedDocument:
    """A document normalized to either text or (bytes + MIME type).

    Exactly one of `text` and `data` is set: `text` for documents that arrive
    as characters, `data` for anything the model has to read visually.
    """

    filename: str
    kind: str  # "text" | "pdf" | "image"
    text: str = ""
    data: bytes | None = None
    mime_type: str = ""
    preview: str = ""
    note: str = ""


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
        return "", f"Could not read text layer ({exc.__class__.__name__}). Sending the PDF as-is."

    if not text:
        return "", "No text layer found - this looks scanned. The model will read it visually."
    return text, ""


def ingest_bytes(data: bytes, filename: str) -> IngestedDocument:
    """Normalize raw file bytes into a provider-neutral document."""
    suffix = Path(filename).suffix.lower()

    if suffix in TEXT_SUFFIXES:
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            raise UnsupportedDocument(f"{filename} is empty.")
        return IngestedDocument(
            filename=filename,
            kind="text",
            text=text,
            preview=text,
        )

    if len(data) > MAX_INLINE_BYTES:
        raise UnsupportedDocument(
            f"{filename} is {len(data) / 1_048_576:.1f} MB. "
            f"The inline limit is {MAX_INLINE_BYTES // 1_048_576} MB — split it or downsample."
        )

    if suffix == ".pdf":
        preview, note = _pdf_text_layer(data)
        return IngestedDocument(
            filename=filename,
            kind="pdf",
            data=data,
            mime_type="application/pdf",
            preview=preview,
            note=note,
        )

    if suffix in IMAGE_MEDIA_TYPES:
        return IngestedDocument(
            filename=filename,
            kind="image",
            data=data,
            mime_type=IMAGE_MEDIA_TYPES[suffix],
            note="Image input - the model reads this visually.",
        )

    supported = sorted(TEXT_SUFFIXES | {".pdf"} | set(IMAGE_MEDIA_TYPES))
    raise UnsupportedDocument(f"Unsupported file type '{suffix}'. Supported: {', '.join(supported)}")


def ingest_path(path: str | Path) -> IngestedDocument:
    path = Path(path)
    return ingest_bytes(path.read_bytes(), path.name)
