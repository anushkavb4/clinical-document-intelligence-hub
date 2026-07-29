"""Comparison of two extractions for the same patient.

No API key needed — the comparison layer, like the triage layer, has no model
dependency.
"""

from __future__ import annotations

import pytest

from src.compare import IMPROVED, UNCHANGED, UNCLEAR, WORSENED, compare
from src.schema import (
    ClinicalExtraction,
    Diagnosis,
    ExtractedValue,
    LabResult,
    Medication,
    Patient,
    VitalSigns,
)
from src.triage import score


def ev(value: str) -> ExtractedValue:
    return ExtractedValue(value=value, confidence="high", source_quote=value)


def vitals(temp="37.0", hr="80", rr="16", sbp="120", dbp="80", spo2="98",
           oxygen="no", consciousness="alert") -> VitalSigns:
    return VitalSigns(
        temperature_c=temp, heart_rate_bpm=hr, respiratory_rate=rr,
        systolic_bp=sbp, diastolic_bp=dbp, spo2_percent=spo2,
        on_supplemental_oxygen=oxygen, consciousness=consciousness, source_quote="",
    )


def lab(name, value, unit="", flag="normal") -> LabResult:
    return LabResult(name=name, value=value, unit=unit, reference_range="",
                     flag=flag, confidence="high", source_quote="")


def med(name, dose="") -> Medication:
    return Medication(name=name, dose=dose, frequency="", route="", indication="",
                      confidence="high", source_quote="")


def record(mrn="NG 88213-4", name="T. Bergstrom", v=None, labs=None, meds=None,
           diagnoses=None) -> ClinicalExtraction:
    return ClinicalExtraction(
        document_type="physician_note",
        patient=Patient(name=ev(name), age=ev("74"), sex=ev("male"), mrn=ev(mrn)),
        encounter_date=ev("22-Jan-2026"), chief_complaint=ev("confusion"),
        diagnoses=diagnoses or [], medications=meds or [], allergies=["none"],
        vitals=v or vitals(), labs=labs or [],
        summary="s", clinical_reasoning="r", recommended_next_steps=[],
        missing_critical_info=[],
    )


def diff(earlier, later):
    return compare(earlier, score(earlier), later, score(later))


def find(changes, label):
    return next(c for c in changes if c.label.lower() == label.lower())


# --------------------------------------------------------------- identity
def test_same_mrn_matches():
    c = diff(record(), record())
    assert c.same_patient and "NG 88213-4" in c.identity_note


def test_mrn_punctuation_differences_still_match():
    c = diff(record(mrn="NG 88213-4"), record(mrn="ng88213 4"))
    assert c.same_patient


def test_different_mrn_is_refused():
    c = diff(record(mrn="NG 88213-4"), record(mrn="NG 55000-1"))
    assert not c.same_patient and "differs" in c.identity_note.lower()


def test_missing_mrn_falls_back_to_surname_and_says_so():
    c = diff(record(mrn="not documented", name="T. Bergstrom (Theo)"),
             record(mrn="not documented", name="Theo Bergstrom"))
    assert c.same_patient and "surname only" in c.identity_note


def test_different_names_without_mrn_is_refused():
    c = diff(record(mrn="not documented", name="Theo Bergstrom"),
             record(mrn="not documented", name="Amara Villanueva"))
    assert not c.same_patient


# ----------------------------------------------------------------- vitals
def test_heart_rate_improvement_is_scored_not_asserted():
    c = diff(record(v=vitals(hr="124")), record(v=vitals(hr="78")))
    hr = find(c.vitals, "Heart rate")
    assert hr.direction == IMPROVED and hr.detail == "NEWS2 2 -> 0"


def test_worsening_vital_is_flagged():
    c = diff(record(v=vitals(rr="16")), record(v=vitals(rr="28")))
    assert find(c.vitals, "Respiratory rate").direction == WORSENED


def test_movement_inside_one_band_is_unchanged():
    """82 -> 88 bpm is a different number but the same clinical band."""
    c = diff(record(v=vitals(hr="82")), record(v=vitals(hr="88")))
    assert find(c.vitals, "Heart rate").direction == UNCHANGED


def test_a_vital_on_only_one_document_is_unclear_not_improved():
    c = diff(record(v=vitals(spo2="90")), record(v=vitals(spo2="not documented")))
    spo2 = find(c.vitals, "SpO2")
    assert spo2.direction == UNCLEAR and "only one document" in spo2.detail


def test_coming_off_oxygen_and_regaining_alertness_register():
    c = diff(record(v=vitals(oxygen="yes", consciousness="voice")),
             record(v=vitals(oxygen="no", consciousness="alert")))
    assert find(c.vitals, "Supplemental oxygen").direction == IMPROVED
    assert find(c.vitals, "Consciousness").direction == IMPROVED


@pytest.mark.parametrize(
    "attr,before,after",
    [("hr", "124", "78"), ("rr", "28", "16"), ("sbp", "88", "124"),
     ("spo2", "90", "98"), ("temp", "38.9", "36.8")],
)
def test_improved_never_means_more_news2_points(attr, before, after):
    """The invariant: this module and the risk band must never disagree."""
    a = record(v=vitals(**{attr: before}))
    b = record(v=vitals(**{attr: after}))
    c = diff(a, b)
    improved = [x for x in c.vitals if x.direction == IMPROVED]
    assert improved, "expected an improvement"
    assert score(b).news2_total < score(a).news2_total


# ------------------------------------------------------------------- labs
def test_critical_lab_returning_to_normal_is_an_improvement():
    c = diff(record(labs=[lab("Lactate", "4.8", "mmol/L", "critical")]),
             record(labs=[lab("Lactate", "1.2", "mmol/L", "normal")]))
    lactate = find(c.labs, "Lactate")
    assert lactate.direction == IMPROVED
    assert "critical -> normal" in lactate.detail and "4.8 -> 1.2" in lactate.detail


def test_lab_leaving_its_range_is_a_deterioration():
    c = diff(record(labs=[lab("CRP", "4", "mg/L", "normal")]),
             record(labs=[lab("CRP", "187", "mg/L", "high")]))
    assert find(c.labs, "CRP").direction == WORSENED


def test_still_abnormal_but_falling_is_unchanged_in_direction():
    """218 -> 142 umol/L is better, but both are flagged high. Say so honestly."""
    c = diff(record(labs=[lab("Creatinine", "218", "umol/L", "high")]),
             record(labs=[lab("Creatinine", "142", "umol/L", "high")]))
    creat = find(c.labs, "Creatinine")
    assert creat.direction == UNCHANGED and "218 -> 142" in creat.detail


def test_labs_present_on_only_one_document_are_skipped():
    c = diff(record(labs=[lab("Lactate", "4.8")]), record(labs=[lab("Troponin", "12")]))
    assert c.labs == []


# ------------------------------------------------------- medications, flags
def test_medication_changes_are_partitioned():
    c = diff(record(meds=[med("Apixaban", "5 mg"), med("Furosemide", "40 mg")]),
             record(meds=[med("Apixaban", "5 mg"), med("Co-amoxiclav", "625 mg")]))
    assert c.medications.retained == ["Apixaban 5 mg"]
    assert c.medications.added == ["Co-amoxiclav 625 mg"]
    assert c.medications.removed == ["Furosemide 40 mg"]


def test_a_changed_dose_reads_as_a_stop_and_a_start():
    """40 mg -> 20 mg is a real change and must not be silently retained."""
    c = diff(record(meds=[med("Furosemide", "40 mg")]),
             record(meds=[med("Furosemide", "20 mg")]))
    assert c.medications.added == ["Furosemide 20 mg"]
    assert c.medications.removed == ["Furosemide 40 mg"]
    assert c.medications.retained == []


def test_critical_flags_split_into_resolved_persisting_and_new():
    a = record(labs=[lab("Lactate", "4.8", "mmol/L", "critical")], v=vitals(spo2="90"))
    b = record(labs=[lab("Lactate", "1.2", "mmol/L", "normal")], v=vitals(spo2="98"))
    c = diff(a, b)
    assert any("Lactate" in f for f in c.flags_resolved)
    assert not any("Lactate" in f for f in c.flags_persisting)


# ------------------------------------------------------------- trajectory
def test_falling_news2_headlines_the_band_change():
    a = record(v=vitals(hr="124", rr="28", spo2="90", sbp="88", temp="38.9",
                        oxygen="yes", consciousness="voice"))
    b = record(v=vitals())
    c = diff(a, b)
    assert c.trajectory == IMPROVED
    assert c.news2_before == 17 and c.news2_after == 0
    assert "HIGH" in c.headline and "ROUTINE" in c.headline


def test_rising_news2_is_reported_as_deterioration():
    c = diff(record(v=vitals()),
             record(v=vitals(hr="124", rr="28", spo2="90", sbp="88")))
    assert c.trajectory == WORSENED


def test_identical_documents_are_unchanged():
    c = diff(record(), record())
    assert c.trajectory == UNCHANGED and "No material change" in c.headline


def test_unscorable_document_falls_back_to_counting_readings():
    """A lab report has no vitals, so NEWS2 cannot carry the comparison."""
    blank = vitals(temp="not documented", hr="not documented", rr="not documented",
                   sbp="not documented", dbp="not documented", spo2="not documented",
                   oxygen="unknown", consciousness="unknown")
    a = record(v=blank, labs=[lab("Potassium", "6.8", "mmol/L", "critical")])
    b = record(v=blank, labs=[lab("Potassium", "4.1", "mmol/L", "normal")])
    c = diff(a, b)
    assert c.news2_before is None and c.news2_after is None
    assert c.trajectory == IMPROVED and "could not be scored" in c.headline
