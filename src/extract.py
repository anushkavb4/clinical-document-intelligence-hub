"""The single Gemini call that turns a document into a typed record.

One request, one schema. The response is constrained to `ClinicalExtraction`
by the API itself and then validated by Pydantic on the way in, so there is
no prose-to-struct step that can drift and no repair loop.

Provider note: this is the only module that knows which vendor is behind the
extraction. `ingest`, `schema`, `prompts`, and `triage` are all provider-neutral.
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass

from google import genai
from pydantic import ValidationError

from .ingest import IngestedDocument
from .prompts import SYSTEM_PROMPT, USER_INSTRUCTION
from .schema import ClinicalExtraction, response_schema

# Multimodal — it reads PDFs and photographs natively, which is what keeps the
# scanned-document path from needing OCR.
#
# gemini-3.1-flash-lite is ~4x faster and has a far more generous free-tier
# allowance, and it produces the same NEWS2 decisions on these documents. It is
# still not the default: on the same five samples it fabricated 3 of 65 source
# quotes, including a Creatinine quote carrying the reference range from the
# row above it. The values were right and the evidence for them was not, which
# is the one trade-off this tool cannot make. See the README.
DEFAULT_MODEL = "gemini-3.6-flash"

API_KEY_HELP = (
    "No API key found. Put GEMINI_API_KEY in your .env file — "
    "a free key takes about a minute at https://aistudio.google.com/apikey"
)

# Transient by nature: a rate limit refills, an overloaded backend recovers.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 20.0


class ExtractionError(Exception):
    """Extraction did not produce a usable record."""


@dataclass
class ExtractionResult:
    record: ClinicalExtraction
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise ExtractionError(API_KEY_HELP)
    return genai.Client(api_key=key)


def _input_blocks(document: IngestedDocument) -> list[dict]:
    """The document itself, then the instruction — in that order.

    Putting the instruction last means the model has read the whole document
    before it reaches the question.
    """
    if document.kind == "text":
        head = {"type": "text", "text": document.text}
    else:
        head = {
            "type": "document" if document.kind == "pdf" else "image",
            "data": base64.standard_b64encode(document.data or b"").decode("ascii"),
            "mime_type": document.mime_type,
        }
    return [head, {"type": "text", "text": USER_INSTRUCTION}]


def _backoff_seconds(exc: Exception, attempt: int) -> float:
    """How long to wait, preferring the delay the API itself suggests."""
    suggested = _suggested_delay(exc)
    if suggested is not None:
        return min(suggested + 0.5, MAX_BACKOFF_SECONDS)
    return min(2.0**attempt, MAX_BACKOFF_SECONDS)


def _suggested_delay(exc: Exception) -> float | None:
    """The wait the API explicitly asked for, if it named one."""
    match = re.search(r"retry in ([0-9.]+)\s*s", str(exc))
    return float(match.group(1)) if match else None


def _explain(exc: Exception, status: int | None) -> ExtractionError | None:
    """Turn a transport-level failure into something a reviewer can act on."""
    if status == 429:
        wait = _suggested_delay(exc)
        when = (
            f"The API asks you to wait about {wait:.0f} seconds."
            if wait
            else "The quota refills within about a minute."
        )
        return ExtractionError(
            f"Rate limited — the free tier's request quota is exhausted. {when} "
            "Nothing is wrong with the document or your API key. If this keeps "
            "happening, switch GEMINI_MODEL in your .env to a model with a larger "
            "free allowance."
        )
    if status in (500, 502, 503, 504):
        return ExtractionError(
            f"The model API is temporarily unavailable (HTTP {status}). "
            "This is transient — retry in a few seconds."
        )
    if status == 401 or status == 403:
        return ExtractionError(
            "The API rejected the key (HTTP {}). Check GEMINI_API_KEY in your .env "
            "file, or issue a new key at https://aistudio.google.com/apikey".format(status)
        )
    return None


def _create(client: genai.Client, model: str, document: IngestedDocument):
    """The API call, retried on the failures that are transient by nature.

    A rate limit is the expected failure on a free tier, not an exceptional
    one — the demo should absorb it rather than surface a stack trace.
    """
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return client.interactions.create(
                model=model,
                system_instruction=SYSTEM_PROMPT,
                input=_input_blocks(document),
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": response_schema(),
                },
                # Clinical documents should not be retained server-side, even
                # synthetic ones. Nothing here needs conversation history.
                store=False,
            )
        except Exception as exc:  # noqa: BLE001 — re-raised below, explained or not
            # The SDK's 429/5xx classes live under a private module, so branch
            # on the status code rather than importing something unstable.
            status = getattr(exc, "status_code", None)
            if status not in RETRYABLE_STATUS:
                raise _explain(exc, status) or exc from exc

            # If the API asks for longer than we are willing to wait, retrying
            # cannot succeed — and on a metered free tier each doomed attempt
            # spends quota that the user needs back. Fail fast and say so.
            suggested = _suggested_delay(exc)
            if suggested is not None and suggested > MAX_BACKOFF_SECONDS:
                raise _explain(exc, status) or exc from exc

            last = exc
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(_backoff_seconds(exc, attempt))

    status = getattr(last, "status_code", None)
    raise _explain(last, status) or last  # type: ignore[misc]


def extract(document: IngestedDocument, model: str | None = None) -> ExtractionResult:
    """Extract a structured clinical record from one ingested document."""
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    client = _client()

    interaction = _create(client, model, document)

    if interaction.status == "incomplete":
        raise ExtractionError(
            "The model ran out of room before finishing the record. The document is "
            "likely too long for a single pass — split it and retry."
        )
    if interaction.status != "completed":
        raise ExtractionError(
            f"Extraction did not complete (status: {interaction.status}). "
            "This is usually a safety filter or a transient API error."
        )

    payload = interaction.output_text
    if not payload:
        raise ExtractionError("The model returned an empty response.")

    try:
        record = ClinicalExtraction.model_validate_json(payload)
    except ValidationError as exc:
        raise ExtractionError(
            f"The response did not validate against the extraction schema: {exc.error_count()} "
            "field error(s)."
        ) from exc

    usage = interaction.usage
    return ExtractionResult(
        record=record,
        model=model,
        input_tokens=getattr(usage, "total_input_tokens", 0) or 0,
        output_tokens=getattr(usage, "total_output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "total_cached_tokens", 0) or 0,
    )
