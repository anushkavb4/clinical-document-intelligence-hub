"""Deterministic comparison of two extractions for the same patient.

Same principle as the triage layer: the model extracts, Python decides. Nothing
here asks a model whether a patient improved. "Improved" has one meaning — the
reading scores fewer NEWS2 points, or the lab moved closer to its own reference
range — and that meaning is testable.

The vitals comparison deliberately imports the same scorers the risk band uses,
so a parameter can never be called "improved" here while contributing more
points there.

Not a medical device. Synthetic data only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import NOT_DOCUMENTED, ClinicalExtraction
from .triage import _SCORERS, TriageResult, parse_number

IMPROVED = "improved"
WORSENED = "worsened"
UNCHANGED = "unchanged"
UNCLEAR = "unclear"

# How far a flagged result sits from its own reference range. Comparing these
# avoids needing a reference-range knowledge base: the lab already told us.
_LAB_SEVERITY = {"normal": 0, "low": 1, "high": 1, "critical": 2}


@dataclass
class Change:
    label: str
    before: str
    after: str
    direction: str
    detail: str = ""


@dataclass
class SetChange:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)


@dataclass
class Comparison:
    headline: str
    trajectory: str
    risk_before: str
    risk_after: str
    news2_before: int | None
    news2_after: int | None
    vitals: list[Change] = field(default_factory=list)
    labs: list[Change] = field(default_factory=list)
    medications: SetChange = field(default_factory=SetChange)
    diagnoses: SetChange = field(default_factory=SetChange)
    flags_resolved: list[str] = field(default_factory=list)
    flags_persisting: list[str] = field(default_factory=list)
    flags_new: list[str] = field(default_factory=list)
    same_patient: bool = True
    identity_note: str = ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _documented(value: str) -> bool:
    return bool(value) and _norm(value) not in {NOT_DOCUMENTED, "unknown", "", "-"}


def _identity(earlier: ClinicalExtraction, later: ClinicalExtraction) -> tuple[bool, str]:
    """Two documents for different patients must never be silently diffed."""
    mrn_a, mrn_b = earlier.patient.mrn.value, later.patient.mrn.value
    if _documented(mrn_a) and _documented(mrn_b):
        # Punctuation and spacing in an MRN vary between systems; digits do not.
        digits = lambda s: re.sub(r"[^0-9a-z]", "", _norm(s))
        if digits(mrn_a) == digits(mrn_b):
            return True, f"Matched on MRN {mrn_a.strip()}."
        return False, f"MRN differs: {mrn_a.strip()} vs {mrn_b.strip()}."

    name_a, name_b = earlier.patient.name.value, later.patient.name.value
    if _documented(name_a) and _documented(name_b):
        # Names are written inconsistently across documents, so compare on
        # surname alone — and say plainly that that is all it is.
        if _surname(name_a) and _surname(name_a) == _surname(name_b):
            return True, "No MRN on both documents - matched on surname only. Verify."
        return False, f"Names differ: {name_a.strip()} vs {name_b.strip()}."

    return False, "Not enough identifying information to confirm these are the same patient."


def _surname(raw: str) -> str:
    """Best-effort surname from the ways a name actually appears on a chart.

    Handles "T. Bergstrom (Theo)", "Bergstrom, Theo", "Mr Theo Bergstrom".
    """
    text = re.sub(r"\([^)]*\)", " ", _norm(raw))  # drop parenthetical nicknames
    text = re.sub(r"\b(mr|mrs|ms|miss|dr|prof|master)\b\.?", " ", text)
    if "," in text:  # "Bergstrom, Theo" puts the surname first
        text = text.split(",")[0]
    parts = [p.strip(".") for p in text.split() if p.strip(". ")]
    return parts[-1] if parts else ""


def _direction_from_points(before: int, after: int) -> str:
    if after < before:
        return IMPROVED
    if after > before:
        return WORSENED
    return UNCHANGED


def _compare_vitals(earlier: ClinicalExtraction, later: ClinicalExtraction) -> list[Change]:
    changes: list[Change] = []
    for label, attr, unit, scorer in _SCORERS:
        raw_a, raw_b = getattr(earlier.vitals, attr), getattr(later.vitals, attr)
        val_a, val_b = parse_number(raw_a), parse_number(raw_b)
        if val_a is None or val_b is None:
            if val_a is not None or val_b is not None:
                changes.append(
                    Change(
                        label,
                        f"{raw_a}{unit}" if val_a is not None else "not documented",
                        f"{raw_b}{unit}" if val_b is not None else "not documented",
                        UNCLEAR,
                        "Recorded on only one document.",
                    )
                )
            continue
        points_a, _ = scorer(val_a)
        points_b, _ = scorer(val_b)
        changes.append(
            Change(
                label,
                f"{_fmt(val_a)}{unit}",
                f"{_fmt(val_b)}{unit}",
                _direction_from_points(points_a, points_b),
                f"NEWS2 {points_a} -> {points_b}",
            )
        )

    if earlier.vitals.consciousness != "unknown" and later.vitals.consciousness != "unknown":
        before_alert = earlier.vitals.consciousness == "alert"
        after_alert = later.vitals.consciousness == "alert"
        changes.append(
            Change(
                "Consciousness",
                earlier.vitals.consciousness,
                later.vitals.consciousness,
                _direction_from_points(0 if before_alert else 3, 0 if after_alert else 3),
                "NEWS2 3 -> 0" if (not before_alert and after_alert) else "",
            )
        )

    oxygen_a, oxygen_b = earlier.vitals.on_supplemental_oxygen, later.vitals.on_supplemental_oxygen
    if "unknown" not in (oxygen_a, oxygen_b):
        changes.append(
            Change(
                "Supplemental oxygen",
                oxygen_a,
                oxygen_b,
                _direction_from_points(2 if oxygen_a == "yes" else 0, 2 if oxygen_b == "yes" else 0),
            )
        )
    return changes


def _compare_labs(earlier: ClinicalExtraction, later: ClinicalExtraction) -> list[Change]:
    by_name_a = {_norm(lab.name): lab for lab in earlier.labs}
    by_name_b = {_norm(lab.name): lab for lab in later.labs}
    changes: list[Change] = []

    for name in sorted(set(by_name_a) & set(by_name_b)):
        a, b = by_name_a[name], by_name_b[name]
        sev_a, sev_b = _LAB_SEVERITY.get(a.flag), _LAB_SEVERITY.get(b.flag)
        if sev_a is None or sev_b is None:
            direction = UNCLEAR
        else:
            direction = _direction_from_points(sev_a, sev_b)

        detail = f"{a.flag} -> {b.flag}"
        num_a, num_b = parse_number(a.value), parse_number(b.value)
        if num_a is not None and num_b is not None and num_a != num_b:
            detail += f"   ({_fmt(num_a)} -> {_fmt(num_b)})"

        changes.append(
            Change(a.name, f"{a.value} {a.unit}".strip(), f"{b.value} {b.unit}".strip(),
                   direction, detail)
        )
    return changes


def _set_change(before: list[str], after: list[str]) -> SetChange:
    index_a = {_norm(x): x for x in before if _documented(x)}
    index_b = {_norm(x): x for x in after if _documented(x)}
    return SetChange(
        added=[index_b[k] for k in index_b if k not in index_a],
        removed=[index_a[k] for k in index_a if k not in index_b],
        retained=[index_b[k] for k in index_b if k in index_a],
    )


def _trajectory(
    before: TriageResult, after: TriageResult, vitals: list[Change], labs: list[Change]
) -> tuple[str, str]:
    """Headline the direction of travel, preferring the band over the total."""
    scored_both = "INDETERMINATE" not in (before.risk_level, after.risk_level)

    if scored_both and after.news2_total != before.news2_total:
        delta = before.news2_total - after.news2_total
        direction = IMPROVED if delta > 0 else WORSENED
        verb = "fallen" if delta > 0 else "risen"
        headline = (
            f"NEWS2 has {verb} from {before.news2_total} to {after.news2_total} "
            f"({before.risk_level} -> {after.risk_level})."
        )
        return direction, headline

    tracked = [c for c in vitals + labs if c.direction in (IMPROVED, WORSENED)]
    better = sum(1 for c in tracked if c.direction == IMPROVED)
    worse = sum(1 for c in tracked if c.direction == WORSENED)

    if not scored_both:
        note = "One document could not be scored, so the comparison rests on individual readings."
        if better > worse:
            return IMPROVED, f"{better} readings improved and {worse} worsened. {note}"
        if worse > better:
            return WORSENED, f"{worse} readings worsened and {better} improved. {note}"
        return UNCLEAR, note

    if better > worse:
        return IMPROVED, (
            f"NEWS2 is unchanged at {after.news2_total}, but {better} individual "
            f"readings improved against {worse} worsening."
        )
    if worse > better:
        return WORSENED, (
            f"NEWS2 is unchanged at {after.news2_total}, but {worse} individual "
            f"readings worsened against {better} improving."
        )
    return UNCHANGED, f"No material change. NEWS2 remains {after.news2_total}."


def compare(
    earlier: ClinicalExtraction,
    earlier_triage: TriageResult,
    later: ClinicalExtraction,
    later_triage: TriageResult,
) -> Comparison:
    """Diff two extractions, treating `earlier` as the baseline."""
    same_patient, identity_note = _identity(earlier, later)

    vitals = _compare_vitals(earlier, later)
    labs = _compare_labs(earlier, later)
    trajectory, headline = _trajectory(earlier_triage, later_triage, vitals, labs)

    flags_a = set(earlier_triage.critical_flags)
    flags_b = set(later_triage.critical_flags)

    return Comparison(
        headline=headline,
        trajectory=trajectory,
        risk_before=earlier_triage.risk_level,
        risk_after=later_triage.risk_level,
        news2_before=None if earlier_triage.risk_level == "INDETERMINATE" else earlier_triage.news2_total,
        news2_after=None if later_triage.risk_level == "INDETERMINATE" else later_triage.news2_total,
        vitals=vitals,
        labs=labs,
        medications=_set_change(
            [f"{m.name} {m.dose}".strip() for m in earlier.medications],
            [f"{m.name} {m.dose}".strip() for m in later.medications],
        ),
        diagnoses=_set_change(
            [d.description for d in earlier.diagnoses],
            [d.description for d in later.diagnoses],
        ),
        flags_resolved=sorted(flags_a - flags_b),
        flags_persisting=sorted(flags_a & flags_b),
        flags_new=sorted(flags_b - flags_a),
        same_patient=same_patient,
        identity_note=identity_note,
    )


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)
