"""Tests for the deterministic risk layer.

The model is not involved here — these fix the NEWS2 arithmetic and the
escalation rules so a prompt change can never silently move a risk band.
Worked examples are taken from the RCP NEWS2 scoring table.
"""

import pytest

from src.schema import (
    ClinicalExtraction,
    ExtractedValue,
    LabResult,
    Medication,
    Patient,
    VitalSigns,
)
from src.triage import parse_number, score


def _value(v="not documented", confidence="high", quote=""):
    return ExtractedValue(value=v, confidence=confidence, source_quote=quote)


def _record(vitals: VitalSigns, *, labs=None, meds=None, allergies=None) -> ClinicalExtraction:
    return ClinicalExtraction(
        document_type="physician_note",
        patient=Patient(name=_value("Test"), age=_value("60"), sex=_value("F"), mrn=_value("X1")),
        encounter_date=_value("2026-01-01"),
        chief_complaint=_value("test"),
        diagnoses=[],
        medications=meds or [],
        allergies=allergies if allergies is not None else ["none known"],
        vitals=vitals,
        labs=labs or [],
        summary="s",
        clinical_reasoning="r",
        recommended_next_steps=[],
        missing_critical_info=[],
    )


def _vitals(**overrides) -> VitalSigns:
    base = dict(
        temperature_c="37.0",
        heart_rate_bpm="70",
        respiratory_rate="16",
        systolic_bp="120",
        diastolic_bp="80",
        spo2_percent="98",
        on_supplemental_oxygen="no",
        consciousness="alert",
        source_quote="",
    )
    base.update(overrides)
    return VitalSigns(**base)


class TestParseNumber:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("38.5", 38.5),
            ("38.5 C", 38.5),
            ("HR 102 bpm", 102.0),
            ("120/80", 120.0),  # systolic is written first
            ("96% on room air", 96.0),
            ("not documented", None),
            ("unknown", None),
            ("", None),
            ("—", None),
        ],
    )
    def test_parses(self, raw, expected):
        assert parse_number(raw) == expected


class TestNews2:
    def test_all_normal_scores_zero(self):
        result = score(_record(_vitals()))
        assert result.news2_total == 0
        assert result.risk_level == "ROUTINE"
        assert result.is_complete

    def test_septic_patient_scores_high(self):
        # RR 28 (3) + SpO2 90 (3) + O2 (2) + temp 38.9 (1) + SBP 88 (3)
        # + HR 124 (2) + voice-responsive (3) = 17
        result = score(
            _record(
                _vitals(
                    temperature_c="38.9",
                    heart_rate_bpm="124",
                    respiratory_rate="28",
                    systolic_bp="88",
                    spo2_percent="90",
                    on_supplemental_oxygen="yes",
                    consciousness="voice",
                )
            )
        )
        assert result.news2_total == 17
        assert result.risk_level == "HIGH"

    def test_boundary_values_score_zero(self):
        # Upper edge of every normal band must not tip into scoring.
        result = score(
            _record(
                _vitals(
                    temperature_c="38.0",
                    heart_rate_bpm="90",
                    respiratory_rate="20",
                    systolic_bp="111",
                    spo2_percent="96",
                )
            )
        )
        assert result.news2_total == 0

    def test_single_red_parameter_escalates_past_its_total(self):
        # One parameter at 3 points, aggregate 3 — bands alone would say LOW.
        result = score(_record(_vitals(respiratory_rate="7")))
        assert result.news2_total == 3
        assert result.risk_level == "MEDIUM"
        assert "red zone" in result.response

    def test_missing_vitals_are_reported_not_assumed(self):
        result = score(
            _record(
                _vitals(
                    respiratory_rate="not documented",
                    spo2_percent="not documented",
                    temperature_c="not documented",
                )
            )
        )
        assert not result.is_complete
        assert result.risk_level == "INDETERMINATE"
        assert "Respiratory rate" in result.unscored

    def test_partial_vitals_flag_the_score_as_a_floor(self):
        result = score(_record(_vitals(respiratory_rate="not documented")))
        assert result.risk_level != "INDETERMINATE"
        assert "floor" in result.response


class TestCriticalFlags:
    def test_critical_lab_surfaces_regardless_of_score(self):
        labs = [
            LabResult(
                name="Lactate",
                value="4.8",
                unit="mmol/L",
                reference_range="0.5-2.2",
                flag="critical",
                confidence="high",
                source_quote="Lactate 4.8",
            )
        ]
        result = score(_record(_vitals(), labs=labs))
        assert result.news2_total == 0
        assert any("Lactate" in f for f in result.critical_flags)

    def test_unasked_allergy_history_is_flagged(self):
        """The literal marker means nobody asked. That is a gap worth raising."""
        result = score(_record(_vitals(), allergies=["not documented"]))
        assert any("allergy" in f.lower() for f in result.critical_flags)

    def test_confirmed_absence_of_allergies_is_not_flagged(self):
        """An empty list means the document records no allergies - per the schema,
        that is an answer, not a gap. Flagging it is a false alarm."""
        result = score(_record(_vitals(), allergies=[]))
        assert not any("allergy" in f.lower() for f in result.critical_flags)

    def test_a_recorded_allergy_is_not_flagged_as_missing(self):
        result = score(_record(_vitals(), allergies=["Penicillin"]))
        assert not any("allergy" in f.lower() for f in result.critical_flags)

    def test_low_confidence_medication_is_flagged_for_verification(self):
        meds = [
            Medication(
                name="Apixaban",
                dose="5 mg",
                frequency="BD",
                route="PO",
                indication="AF",
                confidence="low",
                source_quote="",
            )
        ]
        result = score(_record(_vitals(), meds=meds))
        assert any("Apixaban" in f for f in result.critical_flags)
