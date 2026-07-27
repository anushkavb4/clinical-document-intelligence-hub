# Clinical Document Intelligence Hub

AI prototype that ingests unstructured clinical documents (intake forms, discharge
summaries, lab reports, physician notes) as **text, PDF, or image**, extracts
structured fields, and produces a decision-ready **patient summary card** with a
**risk flag** and a **recommended next step**.

> Proof-of-concept built against a 2-day brief. Synthetic data only — no real PHI.

---

## Status

Scaffold in place. Implementation in progress.

- [ ] Document ingestion (text / PDF / image)
- [ ] Structured extraction (Claude, schema-enforced)
- [ ] Confidence scoring per field
- [ ] Triage / routing logic + risk flag
- [ ] Streamlit UI (summary card)
- [ ] Synthetic sample dataset
- [ ] Five-slide deck

## Planned approach

| Stage | What happens |
| --- | --- |
| **1. Ingest** | PDF → text via `pdfplumber`; images and scanned PDFs → Claude vision; raw text passed through. |
| **2. Extract** | Single Claude call with a forced tool/JSON schema (`src/schema.py`) so every field comes back typed, with a `confidence` score and a verbatim `source_quote` for traceability. |
| **3. Reason** | Deterministic rule layer scores red-flag vitals and labs; the model supplies clinical rationale. Keeping the rules outside the model makes the risk flag auditable. |
| **4. Present** | Streamlit summary card: demographics, problems, medications, abnormal labs, risk band, recommended next step. JSON export for downstream systems. |

## Repository layout

```
├── app.py               # Streamlit UI
├── src/
│   ├── ingest.py        # text / PDF / image → normalized text or image blocks
│   ├── schema.py        # Pydantic models for the structured output
│   ├── prompts.py       # extraction + reasoning prompts
│   ├── extract.py       # Claude API calls, schema-enforced
│   └── triage.py        # risk scoring + routing rules
├── data/samples/        # synthetic clinical documents
├── docs/                # deck, design notes, screenshots
└── outputs/             # generated JSON / cards (gitignored)
```

## Setup

```bash
git clone https://github.com/anushkavb4/clinical-document-intelligence-hub.git
cd clinical-document-intelligence-hub
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # then add your ANTHROPIC_API_KEY
streamlit run app.py
```

## Example input / output

_To be added once extraction is wired up._

## Assumptions

- Synthetic or publicly available documents only; no proprietary or client data.
- Output is decision **support**, not a diagnosis — every extracted field is
  traceable to a quote in the source document so a clinician can verify it.
