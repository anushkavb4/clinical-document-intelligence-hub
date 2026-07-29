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

from src.compare import compare
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


DIRECTION_ICONS = {"improved": "🟢 ↓", "worsened": "🔴 ↑", "unchanged": "⚪ =", "unclear": "🟡 ?"}

TRAJECTORY_STYLES = {
    "improved": ("#1b5e20", "#e8f5e9", "Improving"),
    "worsened": ("#b3261e", "#fce8e6", "Deteriorating"),
    "unchanged": ("#42474e", "#eceff1", "Unchanged"),
    "unclear": ("#a8500b", "#fdf0e3", "Unclear"),
}


def change_table(changes, before_label: str, after_label: str) -> None:
    if not changes:
        st.caption("Nothing recorded on both documents.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "": DIRECTION_ICONS.get(c.direction, ""),
                    "Parameter": c.label,
                    before_label: c.before,
                    after_label: c.after,
                    "Change": c.detail or "—",
                }
                for c in changes
            ]
        ),
        hide_index=True,
        width="stretch",
    )


def trajectory_view(comparison, before_name: str, after_name: str) -> None:
    """What changed between two encounters — computed, not narrated."""
    if not comparison.same_patient:
        st.error(
            f"These do not look like the same patient. {comparison.identity_note} "
            "Comparison suppressed.",
            icon="🛑",
        )
        return

    fg, bg, word = TRAJECTORY_STYLES.get(comparison.trajectory, TRAJECTORY_STYLES["unclear"])
    before_score = "—" if comparison.news2_before is None else comparison.news2_before
    after_score = "—" if comparison.news2_after is None else comparison.news2_after
    st.markdown(
        f"""
        <div style="background:{bg};border-left:6px solid {fg};padding:1rem 1.25rem;
                    border-radius:6px;margin-bottom:1rem;">
          <div style="color:{fg};font-size:1.35rem;font-weight:700;letter-spacing:.02em;">
            {word} &nbsp;·&nbsp; NEWS2 {before_score} → {after_score}
            &nbsp;·&nbsp; {comparison.risk_before} → {comparison.risk_after}
          </div>
          <div style="color:#222;margin-top:.35rem;">{comparison.headline}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"{comparison.identity_note}  ·  Baseline: {before_name} → Later: {after_name}")

    for flag in comparison.flags_new:
        st.error(f"New: {flag}", icon="⚠️")
    for flag in comparison.flags_resolved:
        st.success(f"Resolved: {flag}", icon="✅")
    for flag in comparison.flags_persisting:
        st.warning(f"Still present: {flag}", icon="⏳")

    before_label, after_label = "Baseline", "Later"
    st.subheader("Vital signs")
    change_table(comparison.vitals, before_label, after_label)

    st.subheader("Laboratory results")
    change_table(comparison.labs, before_label, after_label)
    st.caption(
        "Direction follows the flag the laboratory itself assigned. A value falling "
        "while still outside its range reads as unchanged in direction — the number "
        "moved, the clinical category did not."
    )

    cols = st.columns(2)
    with cols[0]:
        st.subheader("Medications")
        for name in comparison.medications.added:
            st.markdown(f"🟢 **started** — {name}")
        for name in comparison.medications.removed:
            st.markdown(f"🔴 **stopped** — {name}")
        for name in comparison.medications.retained:
            st.markdown(f"⚪ continued — {name}")
        if not any(
            [comparison.medications.added, comparison.medications.removed,
             comparison.medications.retained]
        ):
            st.caption("None documented.")
    with cols[1]:
        st.subheader("Diagnoses")
        for name in comparison.diagnoses.added:
            st.markdown(f"🟢 **new** — {name}")
        for name in comparison.diagnoses.removed:
            st.markdown(f"🔴 **no longer listed** — {name}")
        for name in comparison.diagnoses.retained:
            st.markdown(f"⚪ ongoing — {name}")
        if not any(
            [comparison.diagnoses.added, comparison.diagnoses.removed,
             comparison.diagnoses.retained]
        ):
            st.caption("None documented.")


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

UPLOAD_TYPES = ["txt", "md", "csv", "pdf", "png", "jpg", "jpeg", "webp", "gif", "tif", "tiff"]

with st.sidebar:
    st.header("Input")
    sample_files = sorted(SAMPLES_DIR.glob("*")) if SAMPLES_DIR.exists() else []
    sample_names = [f.name for f in sample_files]
    sample_choice = st.selectbox(
        "Sample document",
        ["— none —"] + sample_names,
        help="Synthetic documents bundled with the repo.",
    )
    upload = st.file_uploader("…or upload your own", type=UPLOAD_TYPES)

    st.divider()
    st.subheader("Compare (optional)")
    st.caption(
        "Add an earlier document for the same patient to see what changed "
        "between the two encounters."
    )
    prior_choice = st.selectbox(
        "Earlier document",
        ["— none —"] + sample_names,
        help="The baseline. The document above is treated as the later one.",
        key="prior_sample",
    )
    prior_upload = st.file_uploader("…or upload", type=UPLOAD_TYPES, key="prior_upload")

    st.divider()
    run = st.button("Analyze", type="primary", width="stretch")

    st.caption(
        "Risk scoring uses NEWS2 (Royal College of Physicians), computed "
        "deterministically in Python — not by the model. So is the comparison."
    )


def _resolve(uploaded, choice):
    """Uploaded file wins over the sample dropdown; None if neither is set."""
    if uploaded is not None:
        return ingest_bytes(uploaded.getvalue(), uploaded.name)
    if choice != "— none —":
        return ingest_path(SAMPLES_DIR / choice)
    return None


def _analyze(doc):
    result = extract(doc)
    return result, result.record, score(result.record)


if run:
    try:
        document = _resolve(upload, sample_choice)
        prior_document = _resolve(prior_upload, prior_choice)
    except UnsupportedDocument as exc:
        st.error(str(exc))
        st.stop()

    if document is None:
        st.warning("Pick a sample or upload a document first.")
        st.stop()

    for note in (d.note for d in (prior_document, document) if d and d.note):
        st.info(note)

    try:
        with st.spinner(f"Reading {document.filename}…"):
            result, record, triage = _analyze(document)
        prior_bundle = None
        if prior_document is not None:
            with st.spinner(f"Reading {prior_document.filename}…"):
                prior_result, prior_record, prior_triage = _analyze(prior_document)
            prior_bundle = (prior_document, prior_result, prior_record, prior_triage)
    except ExtractionError as exc:
        st.error(str(exc))
        st.stop()
    except Exception as exc:  # surface API/auth failures rather than a stack trace
        st.error(f"{exc.__class__.__name__}: {exc}")
        st.stop()

    st.session_state["last"] = (document, result, record, triage)
    st.session_state["prior"] = prior_bundle

if "last" in st.session_state:
    document, result, record, triage = st.session_state["last"]
    prior_bundle = st.session_state.get("prior")

    tab_names = ["Summary card", "Extracted detail", "Risk score", "Source", "JSON"]
    if prior_bundle:
        tab_names.insert(1, "Trajectory")
    tabs = st.tabs(tab_names)
    tab = dict(zip(tab_names, tabs))

    if prior_bundle:
        prior_document, prior_result, prior_record, prior_triage = prior_bundle
        with tab["Trajectory"]:
            trajectory_view(
                compare(prior_record, prior_triage, record, triage),
                prior_document.filename,
                document.filename,
            )

    card_tab = tab["Summary card"]
    detail_tab = tab["Extracted detail"]
    score_tab = tab["Risk score"]
    source_tab = tab["Source"]
    json_tab = tab["JSON"]

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
