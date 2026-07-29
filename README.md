# Clinical Document Intelligence Hub

Ingests an unstructured clinical document (**text, PDF, or image**) and returns a
decision-ready **patient summary card**: structured fields with per-field
confidence, a **risk flag**, and **recommended next steps** — every value
traceable to the sentence it came from.

> Proof-of-concept against a 2-day brief. Synthetic data only, no real PHI.
> Decision support, not a medical device.

**Repository:** https://github.com/anushkavb4/clinical-document-intelligence-hub
· **Design notes and measured findings:** [FINDINGS.md](FINDINGS.md)

## Approach

| Stage | What happens | Where |
| --- | --- | --- |
| **1. Ingest** | Text passes through; PDFs and images become raw bytes plus a MIME type, read natively by the model. No local OCR step, so scanned documents take the same path as digital ones. | [src/ingest.py](src/ingest.py) |
| **2. Extract** | **One** Gemini call, constrained to the schema by the API's structured-output mode and re-validated by Pydantic on the way in — no JSON parsing, no repair loop. | [src/extract.py](src/extract.py), [src/schema.py](src/schema.py) |
| **3. Score** | NEWS2 computed in plain Python from the extracted vitals, plus critical-lab and missing-data flags. | [src/triage.py](src/triage.py) |
| **4. Compare** | Optional second document for the same patient, diffed deterministically — vitals by NEWS2 points, labs by the flag the lab itself assigned. | [src/compare.py](src/compare.py) |
| **5. Present** | Summary card, trajectory view, extracted-detail tables, score breakdown, source text, JSON export. | [app.py](app.py) |

**The model extracts; Python decides.** The risk flag is NEWS2 (Royal College of
Physicians) implemented as ordinary code, so a clinician can be shown which
reading contributed which point, the band cannot drift when a prompt is edited,
and it is covered by tests. **Every field carries a verbatim `source_quote`**,
expandable in the UI. **Absent data is stated, never inferred** — missing fields
become the literal string `not documented`, and if three or more NEWS2
parameters are missing the band is `INDETERMINATE` rather than a score built on
partial vitals.

## Tools

`gemini-3.6-flash` via the Gemini Interactions API — multimodal, so it reads
PDFs and photographs natively and the scanned path needs no OCR; and on the free
tier, so the prototype is reproducible without a billing account. Python,
Pydantic for the schema contract, Streamlit for the UI, pdfplumber for the PDF
text preview. The cheaper `gemini-3.1-flash-lite` was measured and rejected —
see [FINDINGS.md](FINDINGS.md).

## Setup

```bash
git clone https://github.com/anushkavb4/clinical-document-intelligence-hub.git
cd clinical-document-intelligence-hub
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env            # then paste in your GEMINI_API_KEY
streamlit run app.py
```

A Gemini API key is free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
— no card required. The free tier caps requests per minute, so the extraction
call retries `429` and transient `5xx`, and if the API asks for a longer wait
than it will sit through it stops rather than spending further quota. Tests need
no API key: `pytest tests -q` (59 passing).

## Example

**Input** — [`data/samples/ed_triage_note_deteriorating.txt`](data/samples/ed_triage_note_deteriorating.txt),
a synthetic ED triage note for a 74-year-old with suspected urosepsis:

```
OBSERVATIONS on arrival 02:51
    Temp        38.9 C (tympanic)      HR   124      RR   28
    BP          88/52                  SpO2 90% on 6 L/min via face mask
    AVPU        V -- responds to voice only, disoriented to time and place
...
    Lactate       4.8  mmol/L   (0.5-2.2)   *** CRITICAL ***
ALLERGIES: son states "none that I know of" -- not confirmed.
```

**Output** — Python scores this **NEWS2 17 → HIGH** (RR 28 → 3, SpO2 90% → 3,
on oxygen → 2, temp 38.9 → 1, systolic 88 → 3, HR 124 → 2, AVPU voice → 3),
with critical flags:

```
Critical lab: Lactate 4.8 mmol/L      Significant hypoxaemia: SpO2 90%
Marked tachypnoea: RR 28/min          Hypotension: Systolic BP 88 mmHg
New confusion or reduced GCS: Consciousness voice
```

The model-generated **summary** from the same run:

> A 74-year-old male with background CKD stage 3b, atrial fibrillation, prostate
> cancer, and recurrent UTIs presented via ambulance with acute confusion,
> reduced oral intake, and slurred speech. On ED arrival, initial observations
> showed marked physiological instability consistent with severe sepsis/septic
> shock: fever (38.9°C), tachycardia (124 bpm), tachypnea (28/min), hypotension
> (88/52 mmHg), hypoxia (SpO2 90% on 6 L/min O2), and altered mental status
> (AVPU 'V'). Bedside laboratory evaluation revealed a critical lactate of
> 4.8 mmol/L, elevated inflammatory markers, acute kidney injury (creatinine
> 218 umol/L), and severe oliguria (15 mL/hr). Sepsis Six protocols have been
> initiated, IV co-amoxiclav and fluid bolus administered, and escalation to the
> medical registrar and critical care outreach is underway.

**Recommended next steps** (most urgent first, abridged): immediate bedside
evaluation by the Medical Registrar and Critical Care Outreach; continue IV
fluid resuscitation and re-assess responsiveness; hourly urine output via
catheter; repeat lactate and blood gas in 1–2 hours.

Note what the model did *not* do: allergies came back as `"Unconfirmed (son
states 'none that I know of')"` rather than "none". The unconfirmed status
survived extraction instead of being flattened into a clean-looking value.

Full JSON for every sample is committed under [outputs/](outputs/).

### Comparing two encounters

Load the same patient's discharge summary as a second document and the app adds
a **Trajectory** view. Same patient, seven days later:

```
IMPROVING · NEWS2 17 → 0 · HIGH → ROUTINE
Matched on MRN NG 88213-4.

Respiratory rate  28/min → 17/min     improved   NEWS2 3 → 0
SpO2              90%    → 96%        improved   NEWS2 3 → 0
Systolic BP       88     → 128 mmHg   improved   NEWS2 3 → 0
Consciousness     voice  → alert      improved   NEWS2 3 → 0
Lactate           4.8    → 1.2 mmol/L improved   critical → normal
Creatinine        218    → 142 umol/L unchanged  high → high  (218 → 142)

Resolved: critical lactate · hypotension · tachypnoea · confusion · hypoxaemia
          · no allergy history documented
Started:  Co-amoxiclav 625 mg, Furosemide 20 mg    Stopped: Furosemide 40 mg
```

Nothing here asks a model whether the patient improved. A vital is "improved"
when it scores fewer NEWS2 points — the same function the risk band uses, so
the two can never disagree — and a lab follows the flag the laboratory itself
assigned, which avoids needing a reference-range knowledge base. Creatinine
falling 218 → 142 while still flagged high reads as *unchanged in direction*,
because the number moved and the clinical category did not.

**Two documents for different patients are never silently diffed.** Identity is
matched on MRN, falling back to surname with an explicit warning, and the
comparison is suppressed outright if neither matches.

## Status

| | |
| --- | --- |
| Ingestion — text / image | done, both exercised live |
| Ingestion — PDF | implemented; verified once against a real 7-page lab PDF, no PDF in the bundled samples |
| Schema-enforced extraction | done — all 5 samples run end-to-end against the live API |
| Source quotes | done; **85/85 traced back to the source, 0 fabricated** |
| Per-field confidence | done, and measured — see [FINDINGS.md](FINDINGS.md) |
| NEWS2 triage + critical flags | done, 20 tests |
| Multi-document comparison | done, 26 tests — identity-checked, deterministic |
| Rate-limit / transient-failure handling | done, 13 tests |
| Streamlit summary card + trajectory view | done |
| Synthetic dataset | 6 documents covering all four types the brief names, including one paired encounter |
| Five-slide deck | done — `Clinical_Document_Intelligence_Hub_deck.pptx` / `.pdf` |
| Recorded demo | not started |

## Assumptions

- Synthetic or publicly available documents only; no proprietary or client data.
- Comparison handles two documents at a time. Longitudinal views across a full
  record are out of scope for the PoC.
- Comparison matches identity on MRN, falling back to surname. Two people
  sharing a surname with no MRN on either document would match — the UI says so
  rather than hiding it.
- NEWS2 is validated for acutely ill adults. It is not appropriate for
  paediatric or obstetric patients, and the tool does not detect those cases — a
  production version would need to gate on patient category.
- A request may return a non-`completed` status (safety filter, token ceiling);
  this is surfaced to the user rather than retried.
- Per-field confidence is reliable at the extremes and noisy in the middle. Treat
  it as a prompt to check, not a measurement.
- Output is decision **support**. Every field is traceable to a quote so a
  clinician can verify it; nothing here should be acted on unreviewed.

## Repository layout

```
├── app.py                        # Streamlit UI
├── src/
│   ├── ingest.py                 # text / PDF / image -> bytes + MIME type
│   ├── schema.py                 # Pydantic contract for the structured output
│   ├── prompts.py                # frozen system prompt
│   ├── extract.py                # the single schema-enforced model call
│   ├── triage.py                 # NEWS2 + critical flags, deterministic
│   └── compare.py                # two-document diff, deterministic
├── tests/                        # 59 tests, no API key required
├── scripts/make_scanned_sample.py  # renders a text sample as a degraded fax
├── data/samples/                 # 6 synthetic documents (5 text, 1 scan)
└── outputs/                      # extraction JSON for each sample
```
