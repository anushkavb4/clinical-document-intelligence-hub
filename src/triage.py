"""Deterministic risk scoring, kept outside the model on purpose.

The model extracts; this module decides. Keeping the risk flag in ordinary
Python means it is reproducible, inspectable, and testable — a clinician can
be shown exactly which reading contributed which point, which is not something
a prose rationale can offer.

The scoring is NEWS2 (Royal College of Physicians National Early Warning Score
2), the standard UK deterioration score. It is used here because it is public,
well documented, and operates on exactly the vitals a discharge summary or
intake form usually carries.

Not a medical device. Synthetic data only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import NOT_DOCUMENTED, ClinicalExtraction

# NEWS2 aggregate score bands -> clinical response.
_BANDS = [
    (0, "ROUTINE", "Routine monitoring per ward policy."),
    (4, "LOW", "Ward-based response. Nurse to assess and decide on escalation."),
    (6, "MEDIUM", "Urgent review by a clinician competent in acute illness."),
    (999, "HIGH", "Emergency assessment by a critical care team. Continuous monitoring."),
]


@dataclass
class ScoreComponent:
    parameter: str
    reading: str
    points: int
    note: str = ""


@dataclass
class TriageResult:
    risk_level: str  # ROUTINE | LOW | MEDIUM | HIGH | INDETERMINATE
    news2_total: int
    response: str
    components: list[ScoreComponent] = field(default_factory=list)
    unscored: list[str] = field(default_factory=list)
    critical_flags: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.unscored


def parse_number(raw: str) -> float | None:
    """Pull the first number out of a free-text reading.

    Handles '38.5', '38.5 C', '120/80' (returns 120), 'HR 102 bpm'.
    Returns None for 'not documented' and anything unparseable.
    """
    if not raw:
        return None
    if raw.strip().lower() in {NOT_DOCUMENTED, "unknown", "n/a", "na", "-", ""}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", raw)
    return float(match.group()) if match else None


def _score_respiratory_rate(v: float) -> tuple[int, str]:
    if v <= 8:
        return 3, "Severe bradypnoea"
    if v <= 11:
        return 1, ""
    if v <= 20:
        return 0, ""
    if v <= 24:
        return 2, ""
    return 3, "Marked tachypnoea"


def _score_spo2(v: float) -> tuple[int, str]:
    if v <= 91:
        return 3, "Significant hypoxaemia"
    if v <= 93:
        return 2, ""
    if v <= 95:
        return 1, ""
    return 0, ""


def _score_temperature(v: float) -> tuple[int, str]:
    if v <= 35.0:
        return 3, "Hypothermia"
    if v <= 36.0:
        return 1, ""
    if v <= 38.0:
        return 0, ""
    if v <= 39.0:
        return 1, ""
    return 2, "Pyrexia"


def _score_systolic(v: float) -> tuple[int, str]:
    if v <= 90:
        return 3, "Hypotension"
    if v <= 100:
        return 2, ""
    if v <= 110:
        return 1, ""
    if v <= 219:
        return 0, ""
    return 3, "Severe hypertension"


def _score_heart_rate(v: float) -> tuple[int, str]:
    if v <= 40:
        return 3, "Bradycardia"
    if v <= 50:
        return 1, ""
    if v <= 90:
        return 0, ""
    if v <= 110:
        return 1, ""
    if v <= 130:
        return 2, ""
    return 3, "Marked tachycardia"


_SCORERS = [
    ("Respiratory rate", "respiratory_rate", "/min", _score_respiratory_rate),
    ("SpO2", "spo2_percent", "%", _score_spo2),
    ("Temperature", "temperature_c", " C", _score_temperature),
    ("Systolic BP", "systolic_bp", " mmHg", _score_systolic),
    ("Heart rate", "heart_rate_bpm", " bpm", _score_heart_rate),
]


def score(extraction: ClinicalExtraction) -> TriageResult:
    """Compute a NEWS2 score and risk band from an extracted record."""
    vitals = extraction.vitals
    components: list[ScoreComponent] = []
    unscored: list[str] = []
    total = 0

    for label, attr, unit, scorer in _SCORERS:
        raw = getattr(vitals, attr)
        value = parse_number(raw)
        if value is None:
            unscored.append(label)
            continue
        points, note = scorer(value)
        total += points
        components.append(ScoreComponent(label, f"{_fmt(value)}{unit}", points, note))

    # Supplemental oxygen: 2 points, and it changes what a given SpO2 means.
    if vitals.on_supplemental_oxygen == "yes":
        total += 2
        components.append(ScoreComponent("Supplemental oxygen", "yes", 2, "On oxygen"))
    elif vitals.on_supplemental_oxygen == "no":
        components.append(ScoreComponent("Supplemental oxygen", "no (room air)", 0))
    else:
        unscored.append("Supplemental oxygen")

    # Consciousness: anything other than alert scores the maximum.
    if vitals.consciousness == "unknown":
        unscored.append("Consciousness (AVPU)")
    elif vitals.consciousness == "alert":
        components.append(ScoreComponent("Consciousness", "alert", 0))
    else:
        total += 3
        components.append(
            ScoreComponent("Consciousness", vitals.consciousness, 3, "New confusion or reduced GCS")
        )

    critical_flags = _critical_flags(extraction, components)
    risk_level, response = _band(total, components, unscored)

    return TriageResult(
        risk_level=risk_level,
        news2_total=total,
        response=response,
        components=components,
        unscored=unscored,
        critical_flags=critical_flags,
    )


def _band(total: int, components: list[ScoreComponent], unscored: list[str]) -> tuple[str, str]:
    """Map an aggregate score to a band, accounting for incomplete vitals."""
    # NEWS2 escalates on any single parameter scoring 3, even at a low total.
    single_red = any(c.points == 3 for c in components)

    for threshold, level, response in _BANDS:
        if total <= threshold:
            break

    if single_red and level in {"ROUTINE", "LOW"}:
        level = "MEDIUM"
        response = "Urgent review: a single parameter is in the red zone."

    # Missing vitals can only understate the score, never overstate it. Say so
    # rather than presenting a partial score as if it were the whole picture.
    if len(unscored) >= 3:
        return "INDETERMINATE", (
            f"Too few vitals documented to score ({len(unscored)} of 7 parameters missing). "
            "Obtain a full observation set before relying on this."
        )
    if unscored:
        response += f" Score is a floor - {len(unscored)} parameter(s) not documented."

    return level, response


def _critical_flags(extraction: ClinicalExtraction, components: list[ScoreComponent]) -> list[str]:
    """Findings that warrant attention regardless of the aggregate score."""
    flags: list[str] = []

    for lab in extraction.labs:
        if lab.flag == "critical":
            flags.append(f"Critical lab: {lab.name} {lab.value} {lab.unit}".strip())

    for component in components:
        if component.points == 3 and component.note:
            flags.append(f"{component.note}: {component.parameter} {component.reading}")

    if not extraction.allergies:
        flags.append("No allergy history documented.")
    elif any(a.strip().lower() == NOT_DOCUMENTED for a in extraction.allergies):
        flags.append("No allergy history documented.")

    low_confidence = [
        m.name for m in extraction.medications if m.confidence == "low"
    ]
    if low_confidence:
        flags.append(
            "Low-confidence medication reading, verify against source: "
            + ", ".join(low_confidence)
        )

    return flags


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)
