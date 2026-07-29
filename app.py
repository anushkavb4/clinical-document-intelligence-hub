"""Clinical Document Intelligence Hub - Streamlit UI.

Upload a clinical document, get a patient summary card with a risk flag and
recommended next steps. Every extracted field can be expanded to show the
verbatim quote it came from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.extract import ExtractionError, extract
from src.ingest import UnsupportedDocument, ingest_bytes, ingest_path
from src.schema import NOT_DOCUMENTED, ClinicalExtraction, ExtractedValue
from src.triage import TriageResult, score

# override=True so editing .env and re-running actually takes effect. Without it
# python-dotenv leaves an already-set variable alone, and a stale GEMINI_MODEL
# survives every Streamlit rerun until the process is killed.
load_dotenv(override=True)

SAMPLES_DIR = Path(__file__).parent / "data" / "samples"

RISK_STYLES = {
    "HIGH": ("#b3261e", "#fce8e6"),
    "MEDIUM": ("#a8500b", "#fdf0e3"),
    "LOW": ("#7a5b00", "#fdf8e3"),
    "ROUTINE": ("#1b5e20", "#e8f5e9"),
    "INDETERMINATE": ("#42474e", "#eceff1"),
}

CONFIDENCE_ICONS = {"high": "🟢", "medium": "🟡", "low": "🔴"}

st.set_page_config(page_title="Clinical Document Intelligence Hub", page_icon="🩺", layout="wide")


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------


def field_row(label: str, value: ExtractedValue) -> None:
    """One extracted scalar with its confidence marker and source quote."""
    icon = CONFIDENCE_ICONS.get(value.confidence, "")
    missing = value.value.strip().lower() == NOT_DOCUMENTED
    shown = "—" if missing else value.value

    st.markdown(f"**{label}**  \n{shown} {'' if missing else icon}")
    if value.source_quote:
        with st.expander("source", expanded=False):
            st.caption(f"“{value.source_quote}”")


def risk_banner(triage: TriageResult) -> None:
    fg, bg = RISK_STYLES.get(triage.risk_level, RISK_STYLES["INDETERMINATE"])
    total = "—" if triage.risk_level == "INDETERMINATE" else triage.news2_total
    st.markdown(
        f"""
        <div style="background:{bg};border-left:6px solid {fg};padding:1rem 1.25rem;
                    border-radius:6px;margin-bottom:1rem;">
          <div style="color:{fg};font-size:1.35rem;font-weight:700;letter-spacing:.02em;">
            {triage.risk_level} &nbsp;·&nbsp; NEWS2 {total}
          </div>
          <div style="color:#222;margin-top:.35rem;">{triage.response}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def score_table(triage: TriageResult) -> None:
    if triage.components:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Parameter": c.parameter,
                        "Reading": c.reading,
                        "Points": c.points,
                        "Note": c.note or "—",
                    }
                    for c in triage.components
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    if triage.unscored:
        st.warning("Not documented, so not scored: " + ", ".join(triage.unscored))


def summary_card(record: ClinicalExtraction, triage: TriageResult) -> None:
    risk_banner(triage)

    if triage.critical_flags:
        for flag in triage.critical_flags:
            st.error(flag, icon="⚠️")

    st.subheader("Patient")
    cols = st.columns(4)
    with cols[0]:
        field_row("Name", record.patient.name)
    with cols[1]:
        field_row("Age", record.patient.age)
    with cols[2]:
        field_row("Sex", record.patient.sex)
    with cols[3]:
        field_row("MRN", record.patient.mrn)

    cols = st.columns(3)
    with cols[0]:
        st.markdown(f"**Document type**  \n{record.document_type.replace('_', ' ').title()}")
    with cols[1]:
        field_row("Encounter date", record.encounter_date)
    with cols[2]:
        field_row("Chief complaint", record.chief_complaint)

    st.divider()
    st.subheader("Summary")
    st.write(record.summary)

    st.subheader("Clinical reasoning")
    st.write(record.clinical_reasoning)

    st.subheader("Recommended next steps")
    if record.recommended_next_steps:
        for i, step in enumerate(record.recommended_next_steps, 1):
            st.markdown(f"{i}. {step}")
    else:
        st.caption("None generated.")

    if record.missing_critical_info:
        st.subheader("Missing information")
        for item in record.missing_critical_info:
            st.markdown(f"- {item}")


def detail_tables(record: ClinicalExtraction) -> None:
    st.subheader("Diagnoses")
    if record.diagnoses:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Diagnosis": d.description,
                        "Status": d.status,
                        "Confidence": f"{CONFIDENCE_ICONS.get(d.confidence, '')} {d.confidence}",
                        "Source": d.source_quote or "—",
                    }
                    for d in record.diagnoses
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("None documented.")

    st.subheader("Medications")
    if record.medications:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Medication": m.name,
                        "Dose": m.dose,
                        "Frequency": m.frequency,
                        "Route": m.route,
                        "Indication": m.indication,
                        "Confidence": f"{CONFIDENCE_ICONS.get(m.confidence, '')} {m.confidence}",
                        "Source": m.source_quote or "—",
                    }
                    for m in record.medications
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("None documented.")

    st.subheader("Labs")
    if record.labs:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Test": lab.name,
                        "Value": f"{lab.value} {lab.unit}".strip(),
                        "Reference": lab.reference_range,
                        "Flag": lab.flag,
                        "Confidence": f"{CONFIDENCE_ICONS.get(lab.confidence, '')} {lab.confidence}",
                        "Source": lab.source_quote or "—",
                    }
                    for lab in record.labs
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("None documented.")

    st.subheader("Allergies")
    st.write(", ".join(record.allergies) if record.allergies else "None documented.")


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------

st.title("🩺 Clinical Document Intelligence Hub")
st.caption(
    "Upload an intake form, discharge summary, lab report, or physician note. "
    "Decision support only — every field is traceable to the source document. "
    "Synthetic data; not a medical device."
)

with st.sidebar:
    st.header("Input")
    sample_files = sorted(SAMPLES_DIR.glob("*")) if SAMPLES_DIR.exists() else []
    sample_choice = st.selectbox(
        "Sample document",
        ["— none —"] + [f.name for f in sample_files],
        help="Synthetic documents bundled with the repo.",
    )
    upload = st.file_uploader(
        "…or upload your own",
        type=["txt", "md", "csv", "pdf", "png", "jpg", "jpeg", "webp", "gif", "tif", "tiff"],
    )
    run = st.button("Analyze document", type="primary", width="stretch")

    st.divider()
    st.caption(
        "Risk scoring uses NEWS2 (Royal College of Physicians), computed "
        "deterministically in Python — not by the model."
    )

if run:
    try:
        if upload is not None:
            document = ingest_bytes(upload.getvalue(), upload.name)
        elif sample_choice != "— none —":
            document = ingest_path(SAMPLES_DIR / sample_choice)
        else:
            st.warning("Pick a sample or upload a document first.")
            st.stop()
    except UnsupportedDocument as exc:
        st.error(str(exc))
        st.stop()

    if document.note:
        st.info(document.note)

    try:
        with st.spinner(f"Reading {document.filename}…"):
            result = extract(document)
    except ExtractionError as exc:
        st.error(str(exc))
        st.stop()
    except Exception as exc:  # surface API/auth failures rather than a stack trace
        st.error(f"{exc.__class__.__name__}: {exc}")
        st.stop()

    record = result.record
    triage = score(record)

    st.session_state["last"] = (document, result, record, triage)

if "last" in st.session_state:
    document, result, record, triage = st.session_state["last"]

    card_tab, detail_tab, score_tab, source_tab, json_tab = st.tabs(
        ["Summary card", "Extracted detail", "Risk score", "Source", "JSON"]
    )

    with card_tab:
        summary_card(record, triage)
    with detail_tab:
        detail_tables(record)
    with score_tab:
        st.subheader("NEWS2 breakdown")
        score_table(triage)
        st.caption(
            "Aggregate bands: 0 routine · 1–4 low · 5–6 medium · ≥7 high. "
            "Any single parameter scoring 3 escalates to at least medium."
        )
    with source_tab:
        st.caption(f"{document.filename} · {document.kind}")
        if document.preview:
            st.text(document.preview)
        else:
            st.caption("No text layer — this document was read visually.")
    with json_tab:
        payload = {
            "extraction": record.model_dump(),
            "triage": {
                "risk_level": triage.risk_level,
                "news2_total": triage.news2_total,
                "response": triage.response,
                "critical_flags": triage.critical_flags,
                "unscored_parameters": triage.unscored,
            },
        }
        st.json(payload)
        st.download_button(
            "Download JSON",
            data=json.dumps(payload, indent=2),
            file_name=f"{Path(document.filename).stem}_extraction.json",
            mime="application/json",
        )

    st.caption(
        f"{result.model} · {result.input_tokens} in / {result.output_tokens} out"
        + (f" · {result.cache_read_tokens} cached" if result.cache_read_tokens else "")
    )
