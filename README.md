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
| **1. Ingest** | Text passes through; PDFs and images become raw bytes plus a MIME type, read natively by the model. No local OCR step, so scanned and photographed documents take the same path as digital ones. `pdfplumber` extracts a text layer purely to show the reviewer what the file contained. | [src/ingest.py](src/ingest.py) |
| **2. Extract** | **One** Gemini call, constrained to the schema by the API's structured-output mode and re-validated by Pydantic on the way in — no JSON parsing, no repair loop. | [src/extract.py](src/extract.py), [src/schema.py](src/schema.py) |
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
├── app.py                        # Streamlit UI
├── src/
│   ├── ingest.py                 # text / PDF / image -> bytes + MIME type
│   ├── schema.py                 # Pydantic contract for the structured output
│   ├── prompts.py                # frozen system prompt
│   ├── extract.py                # the single schema-enforced model call
│   └── triage.py                 # NEWS2 + critical flags, deterministic
├── tests/
│   ├── test_triage.py            # 18 tests pinning the scoring rules
│   └── test_extract_retry.py     # 11 tests on rate-limit / transient handling
├── scripts/
│   └── make_scanned_sample.py    # renders a text sample as a degraded fax
├── data/samples/                 # 5 synthetic documents (4 text, 1 scan)
└── outputs/                      # exported JSON (gitignored)
```

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

A Gemini API key is free and takes about a minute at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) — no card required.
The models used here sit on the free tier.

The free tier caps requests per minute. Analysing several documents in quick
succession can hit it, so [src/extract.py](src/extract.py) retries on `429` and
on transient `5xx`, preferring the delay the API itself suggests. If the quota
is still exhausted after three attempts the UI says so in a sentence rather
than surfacing a stack trace — a rate limit is the expected failure on a free
tier, not an exceptional one.

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

Critical flags raised alongside it:

```
Critical lab: Lactate 4.8 mmol/L
Marked tachypnoea: Respiratory rate 28/min
Significant hypoxaemia: SpO2 90%
Hypotension: Systolic BP 88 mmHg
New confusion or reduced GCS: Consciousness voice
```

The model-generated half of the same run — **summary**:

> A 74-year-old male with background CKD stage 3b, atrial fibrillation, prostate
> cancer, and recurrent UTIs presented via ambulance with acute confusion,
> reduced oral intake, and slurred speech. On ED arrival, initial observations
> showed marked physiological instability consistent with severe sepsis/septic
> shock: fever (38.9°C), tachycardia (124 bpm), tachypnea (28/min), hypotension
> (88/52 mmHg), hypoxia (SpO2 90% on 6 L/min O2), and altered mental status
> (AVPU 'V'). Bedside laboratory evaluation revealed a critical lactate of
> 4.8 mmol/L, elevated inflammatory markers (WBC 17.2 x10^9/L, CRP 187 mg/L),
> acute kidney injury (creatinine 218 umol/L, urea 19.4 mmol/L), and severe
> oliguria (15 mL/hr). Sepsis Six protocols have been initiated, IV co-amoxiclav
> and fluid bolus administered, and escalation to the medical registrar and
> critical care outreach is underway.

**Recommended next steps** (ordered most urgent first, abridged):

1. Immediate bedside evaluation by the Medical Registrar and Critical Care Outreach team (bleeped at 03:04).
2. Continue aggressive IV fluid resuscitation (re-assess fluid responsiveness following initial 500 mL Hartmann's stat) to treat persistent hypotension and hyperlactatemia.
3. Monitor hourly urinary output via urinary catheter to assess renal perfusion.
4. Repeat blood lactate and venous/arterial blood gas in 1–2 hours to track clearance and acid-base status.

Note what the model did *not* do: allergies came back as
`"Unconfirmed (son states 'none that I know of')"` rather than "none", and
"verify allergy history with primary care or pharmacy records" appears in
`missing_critical_info`. The unconfirmed allergy status survived extraction
instead of being flattened to a clean-looking value.

Full JSON for all three samples is written to `outputs/` by a run of the app.

### Provenance check

Across the four text samples, **85 source quotes** were checked against the
documents they claim to come from:

| | |
| --- | ---: |
| Exact substring match | 81 |
| Match after normalising line-wrap whitespace | 4 |
| **Not found in the source document** | **0** |

No fabricated quotes. The four inexact matches are quotes the model joined
across a line break in the source, which is why the check normalises
whitespace before comparing. This check is not decoration — it is what caught
the model regression described below.

## The same document, faxed

[`data/samples/ed_triage_note_scanned.jpg`](data/samples/ed_triage_note_scanned.jpg)
is the ED triage note above rendered the way it would actually arrive at a
hospital — printed, photocopied, skewed on the glass, speckled, run through a
fax leg and saved as a 58-quality JPEG. It is generated reproducibly from the
text by [`scripts/make_scanned_sample.py`](scripts/make_scanned_sample.py)
(fixed seed), which makes it a controlled comparison: same content, two
modalities, so any drift is measurable rather than assumed.

| | Clean text | Scanned JPEG |
| --- | --- | --- |
| Risk band | HIGH | **HIGH** |
| NEWS2 total | 17 | **17** |
| All 7 NEWS2 parameters | — | **identical** |
| All 7 lab values | — | **identical** |
| Critical flags | 5 | **same 5** |
| Document type | `triage_note` | `triage_note` |

Nothing about the risk decision changed. That is the case the pipeline exists
to handle, and it is handled without an OCR stage — the model reads the image
directly.

## Refusing to score

[`data/samples/lab_report_critical_potassium.txt`](data/samples/lab_report_critical_potassium.txt)
is a renal panel carrying a critical potassium of 6.8 mmol/L. It contains no
vital signs at all, which is normal for a lab report and awkward for a scoring
system.

The pipeline does not score it:

```
RISK       : INDETERMINATE
response   : Too few vitals documented to score (7 of 7 parameters missing).
             Obtain a full observation set before relying on this.
unscored   : Respiratory rate, SpO2, Temperature, Systolic BP, Heart rate,
             Supplemental oxygen, Consciousness (AVPU)
critical   : Critical lab: Potassium 6.8 mmol/L
             No allergy history documented.
```

A NEWS2 of 0 would have been arithmetically true and clinically dangerous —
it reads as "this patient is fine" when the truth is "nobody took any
observations." The critical potassium is surfaced anyway, because the
critical-lab path does not depend on having vitals. Declining to answer, while
still raising what matters, is the behaviour worth having.

## What went wrong, and what fixed it

**Confidence scoring was decoration, and measuring it proved that.** The first
version of this prototype reported `high` confidence on essentially everything.
That looks fine until you check whether the signal *moves*. Rendering the same
note at three degradation levels and comparing confidence against ground truth:

| Degradation | Vitals read correctly | Confidence reported |
| --- | ---: | --- |
| As shipped | 6 / 6 | 21 high, 1 medium |
| Degraded | 5 / 6 | **23 high, 0 medium** |
| Severe | 3 / 6 | **20 high, 0 medium** |

At severe degradation the model misread temperature as 37.9 °C instead of 38.9,
and heart rate as 118 instead of 124 — and labelled both `high` confidence. A
clinician reading that card has been told there is nothing to check. That is
worse than showing no confidence at all.

The cause was a prompt that treated confidence as a statement of belief. The
fix, in [src/prompts.py](src/prompts.py), redefines it as a statement about the
*source*: confidence describes how legible and unambiguous the document is, not
how plausible the reading feels — and on a degraded image, every digit that
could be a different digit under this much blur is `medium`, however sure the
model is. After the change:

| Degradation | Vitals read correctly | Confidence reported |
| --- | ---: | --- |
| As shipped | 6 / 6 | 8 high, 16 medium |
| Degraded | 4 / 6 | 6 high, 18 medium |
| Severe | 1 / 6 | **0 high, 17 medium** |

The signal now tracks legibility, and nothing claims `high` on an unreadable
page. The ladder reproduced exactly (8 → 6 → 0) on a second independent run.
The fix is targeted rather than blanket caution — clean digital text is still
105 high / 0 medium across the four text samples, so the change costs nothing
where the source is unambiguous.

**One honest caveat.** The signal is reliable at the extremes and noisy in the
middle. The as-shipped scan produced `8 high / 15 medium` in one run and
`24 high / 0 medium` in another — same bytes, same model, same prompt. Severe
degradation reliably yields zero high-confidence fields; a mildly degraded
scan does not reliably yield anything in particular. Treat per-field
confidence as a prompt to check, never as a measurement.

Worth noting what did *not* move: **NEWS2 came out 17 / HIGH on every run**,
across both modalities and every confidence distribution above. The model's
self-assessment wobbled; the risk decision did not. That is the argument for
keeping the scoring in Python rather than asking the model for a risk band.

## Choosing the model, and measuring the choice

The free tier's request allowance on `gemini-3.6-flash` is small enough to
interrupt a demo, so the obvious move was `gemini-3.1-flash-lite`: roughly
**4× faster** (7 s versus 27 s per document), a far more generous quota, and
the same NEWS2 decision on all five samples. On the results table it looked
like a free win.

The provenance check disagreed. On the same five documents, flash-lite
fabricated **3 of 65 source quotes**. The clearest one:

| | |
| --- | --- |
| Source | `Creatinine    218  umol/L   (60-110)    High` |
| Quote returned | `Creatinine    218  umol/L   (0.5-2.2)   High` |

`(0.5-2.2)` is the reference range for *Lactate*, the row directly above. The
extracted `reference_range` field was correct at `60-110` — the structured
data was right and the evidence offered for it was invented. A clinician
clicking "source" to check that range would have been shown a fabrication.

`gemini-3.6-flash` returned **0 fabricated quotes across 85**. So the default
stays on the slower, more rate-limited model, and flash-lite is documented in
[.env.example](.env.example) as a fallback for staying productive rather than
for demonstrating the audit trail. A tool whose central claim is "every value
is traceable" cannot trade quote fidelity for latency.

**Two smaller findings, not yet fixed:**

- `VitalSigns` carries one `source_quote` for eight values, and on one run the
  model produced a quote that skipped an intervening line (`BM (glucose) 14.2`)
  while reading as continuous. Every fragment was faithful, nothing was
  invented, but a quote that elides silently is a weaker audit trail than one
  that marks the gap. Per-field quotes were exact throughout; the issue is the
  one field where a single quote must span a block.
- The extraction is not deterministic. Across runs the same document yields
  the same NEWS2 score and the same values, but medication frequency is
  sometimes preserved (`BD`) and sometimes expanded (`twice daily`), and one
  run captured the IV fluid as a medication where another did not. Anything
  downstream should key on the structured fields, not on exact strings.

> **Not yet captured:** a UI screenshot and the recorded demo.

## Status

| | |
| --- | --- |
| Ingestion — text / image | done, both exercised live |
| Ingestion — PDF | implemented; verified once against a real 7-page lab PDF, not part of the bundled samples |
| Schema-enforced extraction | done — all 5 samples run end-to-end against the live API |
| Source quotes | done; 85/85 quotes traced back to the source document, 0 fabricated |
| Per-field confidence | done, and **measured** — see *What went wrong* above |
| NEWS2 triage + critical flags | done, 18 tests passing |
| Rate-limit / transient-failure handling | done, 11 tests passing |
| Streamlit summary card | done, boots clean |
| Synthetic sample dataset | 5 documents covering all four types the brief names — 4 text, 1 generated scan |
| Five-slide deck | done — `Clinical_Document_Intelligence_Hub_deck.pptx` / `.pdf` |
| Screenshots / recorded demo | not started |

## Assumptions

- Synthetic or publicly available documents only; no proprietary or client data.
- One document per request. Multi-document comparison and longitudinal views are
  out of scope for the PoC.
- NEWS2 is validated for acutely ill adults. It is not appropriate for
  paediatric or obstetric patients, and the tool does not detect those cases —
  a production version would need to gate on patient category.
- A request may come back with a non-`completed` status (safety filter, token
  ceiling, transient error); this is surfaced to the user rather than retried.
- Output is decision **support**. Every field is traceable to a quote so a
  clinician can verify it; nothing here should be acted on unreviewed.

## Model

`gemini-3.6-flash` by default, overridable via `GEMINI_MODEL`. Chosen because it
is multimodal — it reads PDFs and photographs natively, which is what lets the
scanned-document path skip OCR entirely — and because it is on the free tier,
so the prototype is reproducible by anyone without a billing account. The
cheaper alternative was measured and rejected; see *Choosing the model* above.

Free-tier requests are capped per minute. Analysing several documents quickly
can exhaust the allowance, so the extraction call retries transient failures
and, if the API asks for a longer wait than it is willing to sit through,
stops rather than spending further quota on attempts that cannot succeed.

**Provider is one file.** `extract.py` is the only module that knows which
vendor is behind the extraction; `ingest.py` yields bytes and a MIME type,
`schema.py` is a plain Pydantic contract, and `triage.py` never sees the model
at all. Swapping providers means rewriting roughly ninety lines. That boundary
was worth having: this prototype was originally built against the Anthropic API
and moved to Gemini without touching the schema, the triage rules, the UI, or
a single test.

One provider-specific accommodation is worth noting. Pydantic hoists nested
models into `$defs` and references them with `$ref`, and Gemini's
structured-output mode documents no support for either keyword, so
[`response_schema()`](src/schema.py) inlines the references before the request
goes out rather than relying on undocumented behaviour.
