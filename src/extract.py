"""The single Claude call that turns a document into a typed record.

One request, one schema. `messages.parse()` forces the response to validate
against ClinicalExtraction, so there is no JSON parsing, no repair loop, and
no prose-to-struct step that can drift.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import anthropic

from .ingest import IngestedDocument
from .prompts import SYSTEM_PROMPT, USER_INSTRUCTION
from .schema import ClinicalExtraction

DEFAULT_MODEL = "claude-opus-5"

# Thinking is on by default on Opus 5 and shares this budget with the response,
# so leave real headroom. A truncated extraction is worse than a slow one.
MAX_TOKENS = 16000


class ExtractionError(Exception):
    """Extraction did not produce a usable record."""


@dataclass
class ExtractionResult:
    record: ClinicalExtraction
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


def _client() -> anthropic.Anthropic:
    # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login`
    # profile — in that order. Nothing to hardcode.
    return anthropic.Anthropic()


def extract(document: IngestedDocument, model: str | None = None) -> ExtractionResult:
    """Extract a structured clinical record from one ingested document."""
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    client = _client()

    response = client.messages.parse(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                # The system prompt is identical for every document, so it is
                # worth caching across a batch of uploads.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [*document.blocks, {"type": "text", "text": USER_INSTRUCTION}],
            }
        ],
        output_format=ClinicalExtraction,
    )

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None) or "no explanation given"
        raise ExtractionError(f"The model declined to process this document: {detail}")

    if response.stop_reason == "max_tokens":
        raise ExtractionError(
            "Extraction hit the token limit before finishing. The document is likely "
            "too long for a single pass — split it and retry."
        )

    if response.parsed_output is None:
        raise ExtractionError("The response did not validate against the extraction schema.")

    usage = response.usage
    return ExtractionResult(
        record=response.parsed_output,
        model=response.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
    )
