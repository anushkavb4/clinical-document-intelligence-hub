# Design notes and measured findings

Supporting detail for [README.md](README.md). Everything here was measured
against the live API on the five bundled samples, not estimated.

---

## Three decisions worth calling out

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

---

## Provenance check

Across the four text samples, **85 source quotes** were checked against the
documents they claim to come from:

| | |
| --- | ---: |
| Exact substring match | 81 |
| Match after normalising line-wrap whitespace | 4 |
| **Not found in the source document** | **0** |

No fabricated quotes. The four inexact matches are quotes the model joined
across a line break in the source, which is why the check normalises whitespace
before comparing. This check is not decoration — it is what caught the model
regression described below.

---

## The same document, faxed

[`data/samples/ed_triage_note_scanned.jpg`](data/samples/ed_triage_note_scanned.jpg)
is the ED triage note rendered the way it would actually arrive at a hospital —
printed, photocopied, skewed on the glass, speckled, run through a fax leg and
saved as a 58-quality JPEG. It is generated reproducibly from the text by
[`scripts/make_scanned_sample.py`](scripts/make_scanned_sample.py) (fixed seed),
which makes it a controlled comparison: same content, two modalities, so any
drift is measurable rather than assumed.

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

---

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

A NEWS2 of 0 would have been arithmetically true and clinically dangerous — it
reads as "this patient is fine" when the truth is "nobody took any
observations." The critical potassium is surfaced anyway, because the
critical-lab path does not depend on having vitals. Declining to answer, while
still raising what matters, is the behaviour worth having.

---

## The confidence score was decoration

The first version reported `high` confidence on essentially everything. That
looks fine until you check whether the signal *moves*. Rendering the same note
at three degradation levels and comparing confidence against ground truth:

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
degradation reliably yields zero high-confidence fields; a mildly degraded scan
does not reliably yield anything in particular. Treat per-field confidence as a
prompt to check, never as a measurement.

Worth noting what did *not* move: **NEWS2 came out 17 / HIGH on every run**,
across both modalities and every confidence distribution above. The model's
self-assessment wobbled; the risk decision did not. That is the argument for
keeping the scoring in Python rather than asking the model for a risk band.

---

## Choosing the model, and measuring the choice

The free tier's request allowance on `gemini-3.6-flash` is small enough to
interrupt a demo, so the obvious move was `gemini-3.1-flash-lite`: roughly
**4× faster** (7 s versus 27 s per document), a far more generous quota, and the
same NEWS2 decision on all five samples. On the results table it looked like a
free win.

The provenance check disagreed. On the same five documents, flash-lite
fabricated **3 of 65 source quotes**. The clearest one:

| | |
| --- | --- |
| Source | `Creatinine    218  umol/L   (60-110)    High` |
| Quote returned | `Creatinine    218  umol/L   (0.5-2.2)   High` |

`(0.5-2.2)` is the reference range for *Lactate*, the row directly above. The
extracted `reference_range` field was correct at `60-110` — the structured data
was right and the evidence offered for it was invented. A clinician clicking
"source" to check that range would have been shown a fabrication.

`gemini-3.6-flash` returned **0 fabricated quotes across 85**. So the default
stays on the slower, more rate-limited model, and flash-lite is documented in
[.env.example](.env.example) as a fallback for staying productive rather than
for demonstrating the audit trail. A tool whose central claim is "every value is
traceable" cannot trade quote fidelity for latency.

---

## Two smaller findings, not yet fixed

- `VitalSigns` carries one `source_quote` for eight values, and on one run the
  model produced a quote that skipped an intervening line (`BM (glucose) 14.2`)
  while reading as continuous. Every fragment was faithful, nothing was
  invented, but a quote that elides silently is a weaker audit trail than one
  that marks the gap. Per-field quotes were exact throughout; the issue is the
  one field where a single quote must span a block.
- The extraction is not deterministic. Across runs the same document yields the
  same NEWS2 score and the same values, but medication frequency is sometimes
  preserved (`BD`) and sometimes expanded (`twice daily`), and one run captured
  the IV fluid as a medication where another did not. Anything downstream should
  key on the structured fields, not on exact strings.

---

## Provider portability

`extract.py` is the only module that knows which vendor is behind the
extraction; `ingest.py` yields bytes and a MIME type, `schema.py` is a plain
Pydantic contract, and `triage.py` never sees the model at all. Swapping
providers means rewriting roughly ninety lines. That boundary was worth having:
this prototype was originally built against the Anthropic API and moved to
Gemini without touching the schema, the triage rules, the UI, or a single test.

One provider-specific accommodation is worth noting. Pydantic hoists nested
models into `$defs` and references them with `$ref`, and Gemini's
structured-output mode documents no support for either keyword, so
[`response_schema()`](src/schema.py) inlines the references before the request
goes out rather than relying on undocumented behaviour. The real accepted MIME
types — including TIFF, the format scanned clinical faxes actually arrive in —
were likewise only discoverable in the SDK's own request types, not the
documentation page.
