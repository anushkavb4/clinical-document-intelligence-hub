"""Typed contract for everything the model is allowed to return.

Every request forces the model into this schema, so the UI and the triage
rules can rely on field names and types instead of parsing prose. Two
conventions run through the whole schema:

- No optional fields. Structured outputs require every property to be
  required, so "absent" is expressed as the literal string NOT_DOCUMENTED
  rather than null. Downstream code checks for that constant.
- Every extracted item carries a `confidence` and a `source_quote` — the
  verbatim span from the document it came from. That is what makes the
  summary card auditable by a clinician.
"""

from typing import Literal

from pydantic import BaseModel, Field

NOT_DOCUMENTED = "not documented"

Confidence = Literal["high", "medium", "low"]

DocumentType = Literal[
    "intake_form",
    "triage_note",
    "discharge_summary",
    "lab_report",
    "physician_note",
    "referral",
    "other",
]

DOCUMENT_TYPE_GUIDANCE = (
    "Classify by what the document *is*, not by which fields it happens to "
    "contain. 'intake_form' is a form the patient or clerk fills in before an "
    "encounter; 'triage_note' is written by a nurse or clinician at the point "
    "of arrival, typically in an emergency or urgent care setting; "
    "'physician_note' is a clinician's assessment during an episode of care. "
    "A note recording arrival observations and an initial impression is a "
    "'triage_note' even though it also captures demographics."
)


class ExtractedValue(BaseModel):
    """A single scalar field plus its provenance."""

    value: str = Field(description=f"The extracted value, or '{NOT_DOCUMENTED}'.")
    confidence: Confidence = Field(
        description=(
            "high = stated explicitly and unambiguously; "
            "medium = present but abbreviated, ambiguous, or reformatted; "
            "low = inferred from context rather than stated."
        )
    )
    source_quote: str = Field(
        description=(
            "Verbatim text from the document supporting this value. "
            "Empty string if the value was inferred or is not documented."
        )
    )


class Patient(BaseModel):
    name: ExtractedValue
    age: ExtractedValue
    sex: ExtractedValue
    mrn: ExtractedValue = Field(description="Medical record number or patient ID.")


class VitalSigns(BaseModel):
    """Numeric strings so that unparseable or absent readings survive extraction.

    The triage layer does the number parsing and decides what to do with
    anything it cannot read.
    """

    temperature_c: str = Field(description="Temperature in Celsius. Convert from F if needed.")
    heart_rate_bpm: str
    respiratory_rate: str = Field(description="Breaths per minute.")
    systolic_bp: str
    diastolic_bp: str
    spo2_percent: str = Field(description="Oxygen saturation, percent.")
    on_supplemental_oxygen: Literal["yes", "no", "unknown"]
    consciousness: Literal["alert", "confused", "voice", "pain", "unresponsive", "unknown"] = Field(
        description="AVPU level. Use 'confused' for new confusion or altered mental status."
    )
    source_quote: str = Field(description="Verbatim text of the vitals block, if present.")


class LabResult(BaseModel):
    name: str
    value: str
    unit: str
    reference_range: str
    flag: Literal["low", "normal", "high", "critical", "unknown"] = Field(
        description=(
            "Use the document's own flag when present. Only use 'critical' when the "
            "document marks it critical/panic, or the value is grossly outside range."
        )
    )
    confidence: Confidence
    source_quote: str


class Medication(BaseModel):
    name: str
    dose: str
    frequency: str
    route: str
    indication: str
    confidence: Confidence
    source_quote: str


class Diagnosis(BaseModel):
    description: str
    status: Literal["active", "resolved", "suspected", "ruled_out", "unknown"]
    confidence: Confidence
    source_quote: str


class ClinicalExtraction(BaseModel):
    """The full structured output for one clinical document."""

    document_type: DocumentType = Field(description=DOCUMENT_TYPE_GUIDANCE)
    patient: Patient
    encounter_date: ExtractedValue
    chief_complaint: ExtractedValue
    diagnoses: list[Diagnosis]
    medications: list[Medication]
    allergies: list[str] = Field(description=f"Empty list if none; ['{NOT_DOCUMENTED}'] if not stated.")
    vitals: VitalSigns
    labs: list[LabResult]

    summary: str = Field(
        description=(
            "Three to five sentences a clinician could read in under 20 seconds: "
            "who the patient is, why they presented, what was found, where things stand."
        )
    )
    clinical_reasoning: str = Field(
        description=(
            "Explain what in this document drives concern or reassurance, referencing "
            "specific findings. Do not state a diagnosis the document does not support."
        )
    )
    recommended_next_steps: list[str] = Field(
        description="Concrete, document-grounded actions. Ordered most urgent first."
    )
    missing_critical_info: list[str] = Field(
        description=(
            "Clinically important items absent from this document that a reviewer "
            "would need before acting. Empty list if nothing material is missing."
        )
    )


def _inline_refs(node: object, defs: dict) -> object:
    """Recursively replace every `$ref` with the definition it points at."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref is not None:
            resolved = _inline_refs(defs[ref.rsplit("/", 1)[-1]], defs)
            # A sibling `description` on the reference wins over the target's.
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            return {**resolved, **siblings}  # type: ignore[dict-item]
        return {key: _inline_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def response_schema() -> dict:
    """`ClinicalExtraction` as a JSON Schema with no `$ref` indirection.

    Pydantic hoists every nested model into `$defs` and points at it with a
    `$ref`. Gemini's structured-output mode documents no support for either
    keyword, so the references are inlined here rather than left to chance.
    Safe to recurse: the schema is a tree by construction, and a recursive
    one could not be expressed in structured output anyway.
    """
    schema = ClinicalExtraction.model_json_schema()
    defs = schema.pop("$defs", {})
    return _inline_refs(schema, defs)  # type: ignore[return-value]
