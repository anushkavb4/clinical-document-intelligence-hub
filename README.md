# Clinical Document Intelligence Hub

Ingests an unstructured clinical document (**text, PDF, or image**) and returns a
decision-ready **patient summary card**: structured fields with per-field
confidence, a **risk flag**, and **recommended next steps** — every value
traceable to the sentence it came from.

> Proof-of-concept against a 2-day brief. Synthetic data only, no real PHI.
> Decision support, not a medical device.

---

## Approach

| Stage | What happens | Where |
| --- | --- | --- |
| **1. Ingest** | Text passes through; PDFs and images are sent to Claude as native `document` / `image` blocks. No local OCR step, so scanned and photographed documents take the same path as digital ones. `pdfplumber` extracts a text layer purely to show the reviewer what the file contained. | [src/ingest.py](src/ingest.py) |
| **2. Extract** | **One** Claude call, schema-enforced via `messages.parse()`. The response is validated against a Pydantic model before it is returned — no JSON parsing, no repair loop. | [src/extract.py](src/extract.py), [src/schema.py](src/schema.py) |
| **3. Score** | NEWS2 computed in plain Python from the extracted vitals, plus critical-lab and missing-data flags. | [src/triage.py](src/triage.py) |
| **4. Present** | Summary card, extracted-detail tables, score breakdown, source text, JSON export. | [app.py](app.py) |

### Three decisions worth calling out

**The model extracts; Python decides.** The risk flag is [NEWS2](https://www.rcp.ac.uk/improving-care/resources/national-early-warning-score-news-2/)
(Royal College of Physicians), implemented as ordinary code. A clinician can be
shown exactly which reading contributed which point, the band cannot drift when
a prompt is edited, and it is covered by [tests](tests/test_triage.py). A prose
rationale from an LLM offers none of that.

**Every field carries a `source_quote`.** The model must quote the document
verbatim for each extracted value, or leave the quote empty and mark the field
low-confidence. The UI makes each quote expandable, so verifying a dose takes a
click rather than a re-read of the chart.

**Absent data is stated, never inferred.** Missing fields become the literal
string `not documented`. If three or more NEWS2 parameters are missing the risk
band is reported as `INDETERMINATE` rather than scored on partial vitals; if one
or two are missing the score is labelled a floor. Silent imputation is the
failure mode that would actually hurt someone here.

## Repository layout

```
├── app.py               # Streamlit UI
├── src/
│   ├── ingest.py        # text / PDF / image -> API content blocks
│   ├── schema.py        # Pydantic contract for the structured output
│   ├── prompts.py       # frozen system prompt (stable cache prefix)
│   ├── extract.py       # the single schema-enforced Claude call
│   └── triage.py        # NEWS2 + critical flags, deterministic
├── tests/test_triage.py # 18 tests pinning the scoring rules
├── data/samples/        # 3 synthetic documents
└── outputs/             # exported JSON (gitignored)
```

## Setup

```bash
git clone https://github.com/anushkavb4/clinical-document-intelligence-hub.git
cd clinical-document-intelligence-hub
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env            # then add your ANTHROPIC_API_KEY
streamlit run app.py
```

Run the tests (no API key needed — the scoring layer has no model dependency):

```bash
pytest tests -q
```

## Example

**Input** — [`data/samples/ed_triage_note_deteriorating.txt`](data/samples/ed_triage_note_deteriorating.txt),
a synthetic ED triage note for a 74-year-old with suspected urosepsis:

```
OBSERVATIONS on arrival 02:51
    Temp        38.9 C (tympanic)
    HR          124
    RR          28
    BP          88/52
    SpO2        90% on 6 L/min via face mask
    AVPU        V -- responds to voice only, disoriented to time and place
...
    Lactate       4.8  mmol/L   (0.5-2.2)   *** CRITICAL ***
ALLERGIES: son states "none that I know of" -- not confirmed.
```

**Output** — the deterministic half of the pipeline scores this **NEWS2 17 →
HIGH**, verified by test:

| Parameter | Reading | Points |
| --- | --- | ---: |
| Respiratory rate | 28 /min | 3 |
| SpO2 | 90% | 3 |
| Supplemental oxygen | yes | 2 |
| Temperature | 38.9 C | 1 |
| Systolic BP | 88 mmHg | 3 |
| Heart rate | 124 bpm | 2 |
| Consciousness | voice | 3 |
| **Total** | | **17** |

→ *Emergency assessment by a critical care team. Continuous monitoring.*
Critical flags raised alongside it: `Critical lab: Lactate 4.8 mmol/L`,
`Significant hypoxaemia`, `Hypotension`, `No allergy history documented.`

> **Not yet captured:** the model-generated half of this example (summary,
> clinical reasoning, next steps) and a UI screenshot. Both need a live
> `ANTHROPIC_API_KEY`, which was not available on the build machine — the
> extraction path is implemented and statically verified but has not been run
> against the API. See *Status* below.

## Status

| | |
| --- | --- |
| Ingestion — text / PDF / image | done |
| Schema-enforced extraction | implemented, **not yet run against the live API** |
| Per-field confidence + source quotes | done |
| NEWS2 triage + critical flags | done, 18 tests passing |
| Streamlit summary card | done, boots clean |
| Synthetic sample dataset | 3 documents |
| Screenshots / recorded demo | pending a live run |
| Five-slide deck | not started |

## Assumptions

- Synthetic or publicly available documents only; no proprietary or client data.
- One document per request. Multi-document comparison and longitudinal views are
  out of scope for the PoC.
- NEWS2 is validated for acutely ill adults. It is not appropriate for
  paediatric or obstetric patients, and the tool does not detect those cases —
  a production version would need to gate on patient category.
- The model may decline a document (`stop_reason: "refusal"`); this is surfaced
  to the user rather than retried.
- Output is decision **support**. Every field is traceable to a quote so a
  clinician can verify it; nothing here should be acted on unreviewed.

## Model

`claude-opus-5` by default, overridable via `ANTHROPIC_MODEL`. Chosen for the
clinical reasoning quality that carries the largest share of the rubric; the
extraction path is model-agnostic, so a cost-sensitive deployment could drop to
Sonnet for high-volume routine documents and reserve Opus for complex notes.
