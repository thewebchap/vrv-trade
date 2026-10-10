# TradePilot: trade document verification (metals)

TradePilot checks the documents of one metals trade against the company's payment checklist. For each check it returns **PASS**, **PASS_WITH_NOTE**, **REVIEW**, **MISMATCH** or **FAIL**, and backs every result with evidence (file, page, source line, raw value, normalised value and how it was obtained).

It runs entirely on the local machine. PDFs are read with PyMuPDF, scanned pages are OCR'd by the macOS Vision framework, and a local **Qwen3-VL 8B** model served by **Ollama** does the reading. No document content leaves the machine.

> Current scope: **metals only**. A trade can come in two ways: from a local folder (CLI), or by email to the Outlook mailbox from an `@vrvglobal.com` sender. Both go through the same processor and produce the same reports. Each trade is stored under a `TRD-YYYY-NNNNNN` reference and gets one row in a master Excel register. There is no API, UI or deployment.

## Guiding principle

```
AI UNDERSTANDS.  CODE NORMALISES.  RULES VERIFY.  EVIDENCE EXPLAINS.  UNCERTAINTY GOES TO REVIEW.
```

The model is never asked whether the documents match. It is asked to *read* specific values, or to state one narrow fact. Code checks that each value really appears in the document, normalises it and evaluates the rule.

## Repository layout

```
tradepilot.py            CLI entry point
tradepilot/
  config.py              environment variables
  models.py              Document, Field (with provenance), CheckResult
  pdf_text.py            native text per page; OCR only for image pages; line reconstruction; cache
  classify.py            document type from content (+ filename as corroboration only)
  local_model.py         the only code that talks to Ollama; never raises; disk cache
  extract.py             schema prompts per document family; grounding; label confirmation; signatures (vision)
  normalize.py           numbers (US/EU/Indian/OCR slips), units, dates, companies, Incoterms, currency, amount-in-words
  canonical.py           canonical trade view: quantities in MT with basis (net/gross/count) and printed precision,
                         prices in currency/MT with basis, contract materials/tolerances/price tolerance
  commercial.py          quantity and price verification (the core checks) with full per-comparison evidence
  checklist.py           loads the Excel checklist; reviewed interpretation per row; payment-term matching
  checks.py              checklist and business-rule checks (documents, advances, notes, parties, goods...)
  pricing.py             exchange-linked pricing formulas: parse and compare, never price
  commodity.py           ISRI grades, metal/form identity, physical recovery
  verify.py              runs all checks, grouped by checklist item
  pipeline.py            one case / batch orchestration
  report.py              result.json, report.md, report.xlsx, email_response.txt (deterministic), batch summary
  processor.py           process_trade(): the one entry point used by local mode and email mode
  trade_store.py         trade folders, TRD reference allocation, source PDFs, metadata, master register
  state.py               SQLite record of inbox messages (duplicate protection, reply audit)
  mailbox.py             Outlook worker: sender policy, PDF-only intake, trade creation, threaded reply
  outlook.py             Microsoft Graph (delegated OAuth, token cache); imported only in Outlook mode
tests/                   269 tests, incl. the 50 business-rule cases and the Outlook/storage workflow
                         (no model, OCR, Microsoft login or network needed)
Documents/
  Basic Checklist.xlsx   the checklist (business source of truth)
  Metals/1..10/          one folder per trade: 1. PC, 2. PI, 3. BL, 4. SC, 5. SI
  Rubber/                out of scope
```

## The data, and what the checklist really asks

Each metals case is a back-to-back trade. VRV buys from a supplier under a **Purchase Contract (PC)** and is invoiced on a **Purchase Invoice (PI)**. The goods ship on a **Bill of Lading (BL)**. VRV sells to an end buyer under a **Sales Contract (SC)** and bills them on a **Sales Invoice (SI)**. 28 of the 50 PDFs are image-only (every BL, most SCs and SIs). Cases 4/5, 6/7 and 8/9 are two shipments under the same contracts.

The checklist (`Sheet4`) is not a list of field comparisons. It has:

* **COMMON CHECKS 1–8**: signatures and stamps, either party's contract may be used, advances deducted, debit notes, bank-detail changes, buyer advance before supplier advance, exposure limits, and **GP% ≥ 2% for metals**.
* **METALS 1–9**: each row is a *payment term* (for example "100% TT ON SCAN COPY OF DBL, AFTER VESSEL SAILING"). Its instruction lists the documents to check at each payment stage and ends with "Ensure all documents match and there are no discrepancies".
* **RUBBER 1–9**: ignored.

So the engine:
1. reads the purchase contract's payment clause;
2. selects the matching METALS row;
3. works out the payment stage and required documents;
4. breaks "all documents match" down into explicit cross-document checks.

The workbook has no severity or priority column, so all items carry equal weight.

## Business rules (validation engine)

**Statuses.** Severity runs FAIL > MISMATCH > REVIEW > PASS_WITH_NOTE > PASS, and the overall result is the most severe item.

| Status | Meaning |
|---|---|
| **FAIL** | A mandatory document is missing |
| **MISMATCH** | A definite, material discrepancy has been established |
| **REVIEW** | A required condition cannot be determined reliably |
| **PASS_WITH_NOTE** | Accepted, with a stated limitation |
| **PASS** | Checked and matched |

* **Mandatory documents.** PC, PI, SC, SI and BL are identified by content, never by filename alone. A document that is genuinely absent → **FAIL**. One that is attached but unreadable or unidentified → **REVIEW**; it is never reported as missing.
* **Combined PDFs** are split at pages where a new document starts. A packing list, delivery order or shipping instruction is never taken as a BL.
* **Supporting documents are optional.** Packing lists, certificates, debit/credit notes, payment advices and so on → PASS_WITH_NOTE when absent.
  * Documents a contract lists under "Documents to be presented" are reported separately as contractual presentation documents.
  * Documents the checklist row requires for the payment stage (e.g. packing list and AIFTA for METALS-9) → REVIEW when absent.
* **Commercial legs and scope** (`tradepilot/scope.py`). Every check and comparison carries a scope, shown in the report:
  * **PURCHASE** (PC ↔ PI) and **SALES** (SC ↔ SI): commercial terms (price, Incoterm, payment terms, advances, deductions, currency, contract references) are compared within their leg only. A guard refuses any cross-leg comparison of these fields.
  * **CROSS_TRADE**: physical facts (goods, grade, quantity, BL, vessel, ports, dates), plus checklist rules that explicitly span the trade (GP% margin, buyer-advance-first).
  * **CASE**: document completeness and signatures.
  * Buying CFR and selling CIF is a normal trade.
* **Operative Incoterm.** Only labelled delivery-term fields ("Delivery Terms", "Terms of Delivery", "Incoterm", "Shipment Term", "Price basis") are operative. Explanatory or template text ("Incoterms 2020 rule … for example, FOB, CFR or CIF") is ignored. Two different operative terms in one document → REVIEW with both statements as evidence. A contract/invoice difference within one leg → MISMATCH.
* **Purchase and sales are financially independent.** PC↔PI and SC↔SI are each validated on their own. Purchase and sales prices, formulas, quotation periods, advances and payment terms are never compared for equality. The checklist's GP ≥ 2% rule is a margin threshold and is kept.
* **Fixed prices** are fully validated: unit price against the contract, and quantity × contract price = invoiced total. All money arithmetic uses Python `Decimal`.
* **Exchange-linked prices** (LME/"LLME", COMEX/CME, SHFE, ICE, LBMA) are decomposed into exchange, metal, benchmark, pricing factors, recovery/content factors and premiums.
  * They are accepted as **PASS_WITH_NOTE**: "Exchange-based pricing accepted; external market benchmark not independently verified."
  * If an invoice restates the formula, every element it states must match the contract, or the result is **MISMATCH**.
  * An invoice that states only a final price is accepted at that price, and quantity × declared price = total is still checked.
  * No market price is ever fetched, and the price checks make no network calls (enforced by a test).
* **Advances** are validated per side against that side's contract:
  * the arithmetic net = gross − advance must hold;
  * a deduction above the invoice value → MISMATCH;
  * a deduction above the contractual percentage → REVIEW;
  * a contract advance with no deduction → REVIEW, unless the contract defers it to the last shipment;
  * a deduction within the contract → PASS_WITH_NOTE, because the payment itself is not verified.
* **Debit/credit notes.** None supplied or referenced → PASS_WITH_NOTE. A note referenced on an invoice but not supplied → REVIEW. A supplied note is matched to its invoice, its direction is derived from who issued it, and duplicates are detected. An ordinary advance deduction never requires a debit note.
* **Physical reconciliation:**
  * ISRI grades are compared deterministically (Druid, Twang, Rains, Berry, Candy…), and a different grade or metal → MISMATCH;
  * physical recovery (a quality specification) is checked separately from commercial payable factors (pricing);
  * BL parties are judged by role: the shipper may be a producer or forwarder, and the consignee may be "TO ORDER" or a bank;
  * a BL that states only a gross weight is used as net only when the documents show no packaging weight (bulk/loose cargo, or an invoice stating gross = net).
* **Checklist items needing external records** (exposure limits, payment sequencing) → PASS_WITH_NOTE stating they were **not verified**.

## Pipeline

```
case folder
 → discover PDFs
 → text per page (PDF text layer, else OCR at 300 dpi)              deterministic
 → classify (content signals + own-company role; filename corroborates)  deterministic
 → extract raw values per document schema                            Qwen (text)
 → ground each value in the page text (page, line, label check)     deterministic
 → normalise (numbers by document locale, units → MT, dates, names)  deterministic
 → canonical view (qty × price = amount reconciliation, USD/MT)      deterministic
 → load checklist + reviewed interpretation; match payment term      deterministic
 → checks                                                            deterministic + narrow AI
 → PASS / PASS_WITH_NOTE / REVIEW / MISMATCH / FAIL per check and per checklist item
 → result.json, report.md, report.xlsx
```

### What is deterministic and what uses the model

| Step | How |
|---|---|
| Text, OCR, classification, payment-term matching, required documents | Deterministic |
| Field extraction across 15+ layouts (5 PI formats, 6 carriers' BLs) | **Qwen**, raw values only; code grounds and normalises every value |
| IDs, HS codes, currency, Incoterms, dates, quantities, prices, tolerances, arithmetic, GP% | Deterministic |
| Signature and stamp presence on contract signature pages | Visual, per signatory block (`signatures.py`): block located from its label; non-text ink measured deterministically; **Qwen vision** asked about the cropped block, with a 300 dpi second look when a stamp is seen without a signature; an inked "For &lt;Company&gt;" stamp read by OCR in coloured ink also counts as a stamp. A model "present" answer is accepted only if non-text ink corroborates it. Presence only; identity/authenticity is not checked |
| Goods descriptions with different wording ("Insulated Aluminium Wire Scrap" vs "ALUMINUM SCRAP TWANG") | **Qwen**, only when normalised text differs; grade conflicts (304 vs 316) are deterministic |
| "Any port in Chile" vs "SAN ANTONIO" | **Qwen** supplies one fact (the port's country, using BL context); code compares it |
| Debit notes, exposure limits, payment ledger | Not in the documents → always REVIEW |

### OCR pipeline

* **Native text first.** A page's PDF text layer is used when it is reliable. Pages with no text layer, unreadable glyphs or a broken encoding are OCR'd.
* **Three local OCR passes per scanned page** (macOS Vision):
  * the unprocessed 300 dpi render is the primary text;
  * a 400 dpi grayscale + autocontrast + deskew render and a 200 dpi render give independent second opinions.
* **Reconciliation of the primary text before the model reads it:**
  * a number that both other passes read with different digits takes their reading (`99.199` → `99.499` in case 7);
  * a line both other passes contain but the primary lost is inserted;
  * confusable letters *inside* numbers are fixed (`1O0` → `100`, `2O25` → `2025`, `5O%` → `50%`).
  * Identifiers and words are never rewritten at this stage (`S00004` stays as printed).
* **Why preprocessing is not the primary text.** It fixes faint print but darkens watermarks into text: under a diagonal ORIGINAL stamp it turned a goods description into noise, and Vision's confidence did not flag it. Otsu binarisation made results worse on these documents, so it is not used.
* **Numeric fields.** Quantity, weight, price, amount, percentage and date fields additionally correct O/o→0, I/l/|→1, S→5, B→8, Z→2 in numeric-looking tokens (`I00` → `100`). Month names and text are left alone.
* **Identifiers** are only corrected with outside support:
  * a confusable-only difference against a **native-text** document resolves to the native value;
  * between two OCR'd documents it resolves only if another OCR pass of the same page reads it the other way;
  * otherwise it stays REVIEW;
  * two native documents that differ are a real mismatch.
* **Garbled labels.** When OCR garbles a label (e.g. "B/L No." read as "ALM."), the related document's identifier is searched for in the text.
* **Every correction is kept:** the raw OCR value, the corrected value, and `ocr_corrections` (`from`, `to`, `reason`) on the field and on the page. `report.md` lists them in an "OCR corrections" section.

### Safeguards against model and OCR errors

* **Grounding.** Every extracted value must be found in the document text, or it is marked `AMBIGUOUS`. The matching line becomes the evidence.
* **Numbers are grounded as numbers.** 26.0615 MT is not matched to "26061.5 KGS" just because the digits coincide.
* **Label confirmation.** Ports, vessel, voyage, on-board date, BL number, contract reference, per-BL quantity, **net weight** and **gross weight** must sit next to a matching label. Without one the value is `AMBIGUOUS`. (The model once put the SI's delivery place under "port of loading"; this check catches that.)
* **Locale-aware numbers.** `26.001,5` KG and `USD 7,26600`/KG are EU format, and `1,92,075.04` is Indian lakh grouping. A string that is genuinely ambiguous keeps both readings, and the line arithmetic decides which is right.
* **Arithmetic reconciliation.** qty × unit price = amount, lines = gross, gross − advance = net, and amount in words = total. On OCR'd pages a single-digit difference becomes REVIEW, not FAIL.
* **The model can never cause a MISMATCH.** An AI "no" or "unsure" becomes REVIEW.
* **Interpretation drift.** Each checklist row's interpretation is pinned to its original wording. If someone edits the workbook text, the item becomes REVIEW until the interpretation is re-reviewed.

## Canonical data model

Every extracted value is a `Field`:

```json
{"canonical_field": "net_weight", "raw_value": "26.0015", "normalized_value": 26.0015,
 "document_type": "BILL_OF_LADING", "document": "3. BL.pdf", "page": 1,
 "source_text": "TOTAL NET WEIGHT: 26.0015 MTS", "extraction_method": "llm_on_ocr_text", "status": "FOUND"}
```

Status is one of `FOUND`, `NOT_FOUND` or `AMBIGUOUS` (with a `note`). No numeric confidence scores are invented. The fields are only those the checklist needs:

* **Contracts:** number, date, parties, materials (description, HS, qty, tolerance, price, fixed/formula), per-BL lot, partial shipment, shipment period, delivery terms, ports, currency, seller bank details, payment clause.
* **Invoices:** number, date, parties, contract and BL references, line items, gross/advance/net, amount in words, vessel, ports, on-board date, bank details.
* **Bill of lading:** number, shipper, notify party, vessel/voyage, ports, on-board date, goods description, HS code, net/gross weight, ORIGINAL/DRAFT marking.

## Quantity and price verification (core, every case)

Quantity and price are verified on every case, whichever checklist rows apply. They are reported as the checklist item `CORE-QTY-PRICE`, and the METALS row's "all documents match" references the same checks.

| Check | What it does |
|---|---|
| `QTY.MEASURES` | Records which measure each document states (net weight, gross weight, a weight without net/gross label, or a count) and checks net ≤ gross. A BL with only a gross weight is REVIEW. |
| `QTY.SHIPPED` | PI quantity = BL net weight = SI quantity. Values are compared at the precision each document prints (half the coarser step), so 20,031.3 kg vs 20,031.5 kg is a real difference. On an OCR'd page, a one-digit difference is REVIEW. |
| `QTY.CONTRACT` | PI vs PC, SI vs SC **and BL net weight vs PC and SC**, within the contract tolerance. Applies the quantity per BL/lot, partial-shipment terms and per-material allocations. Example: PC 100 MT ±5%, BL net 103 MT → PASS. |
| `PRICE.CONTRACT` | Invoice unit price (normalised to currency/MT, per-KG prices converted) = contract price, within the contract's price tolerance if one is stated. Different currencies → FAIL; formula/LME prices → REVIEW. |
| `AMOUNT.EXPECTED` | Independent recalculation: invoice quantity × **contract** unit price = expected total, compared with the invoiced goods value. The same calculation on the BL net weight catches invoicing more than was shipped. Example: 100 MT × USD 2,000 = 200,000; an invoice of 205,000 → FAIL with the +5,000 difference. |
| `ARITH.INVOICES` | Internal arithmetic of each invoice: quantity × the invoice's own price, line totals, gross − advance = net, amount in words. |

**Rules for quantities:**
* Net weight, gross weight and counts are never interchanged.
* A net weight is accepted only when it is printed with a NET / N.W. label.
* A gross weight may come from a "Gross Weight" column header.
* A bare "Quantity: 26 MT" is treated as the commercial (invoiced) weight, which for metals is the net weight. The evidence says so.
* Counts (pieces, bales, packages) are reported but never compared with weights.

**Evidence for every comparison:**
* `left` and `right` values: document, page, raw wording, raw value, normalised value, unit, basis and currency;
* the tolerance used;
* the calculation performed;
* the result.

`report.md` shows a "Quantities and prices" table per case; `report.xlsx` has *Quantities & prices* and *Comparisons* sheets.

## Checks

| Checklist item | Check id(s) | Type |
|---|---|---|
| COMMON-1 signed and stamped (China/HK: seal alone) | `SIG.CONTRACTS` | Vision AI (missing → REVIEW) |
| COMMON-2 either party's contract | `REF.CONTRACT` | Deterministic |
| COMMON-3 advances deducted | `ADV.DEDUCTED` | Conditional |
| COMMON-4 debit notes | `EXT.DEBIT_NOTES` | External data → REVIEW |
| COMMON-5 bank details changed | `BANK.UNCHANGED` | Deterministic (difference → REVIEW: needs Trade Ops) |
| COMMON-6 buyer advance first | `ADV.BUYER_FIRST` | Conditional |
| COMMON-7 exposure limit | `EXT.EXPOSURE` | Conditional / external data |
| COMMON-8 GP% ≥ 2 | `GP.MIN` | Normalised (per MT from invoices) |
| CORE-QTY-PRICE (always) | `QTY.MEASURES`, `QTY.SHIPPED`, `QTY.CONTRACT`, `PRICE.CONTRACT`, `AMOUNT.EXPECTED`, `ARITH.INVOICES` | Normalised / tolerance / arithmetic |
| METALS-n (selected payment term) | `PAYTERM.MATCH`, `DOCS.REQUIRED`, `VESSEL.SAILED`, the CORE-QTY-PRICE checks, `REF.BL`, `GOODS.HS`, `GOODS.DESCRIPTION`, `PARTIES`, `ROUTING.PORTS`, `SHIP.VESSEL`, `SHIP.DATE`, `TERMS.INCOTERM`, `TERMS.CURRENCY` | Mixed |

**Result rules:**
* A checklist item takes the worst result of its checks (FAIL > MISMATCH > REVIEW > PASS_WITH_NOTE > PASS).
* A case takes the worst result of its items: FAIL only for a missing mandatory document.
* The summary separately lists items that are REVIEW *only* because they need data outside the documents.

## Model choice: `qwen3-vl:8b-instruct`

* **One model for text and vision.** It reads OCR text and judges signatures and stamps on page images.
* **Fits a 16 GB Apple-silicon laptop.** About 6 GB at Q4_K_M, fully on the GPU.
* **Fast enough.** Measured on an M2: about 27 s to extract one invoice, about 15 s per signature page. Results are cached.
* **Reliable JSON** with Ollama's `format: json` at temperature 0.
* **Same model on-prem.** It runs unchanged on a GPU server; switch to `qwen3-vl:32b` there with `LOCAL_MODEL_NAME`.
* **Use the `-instruct` tag.** The default `qwen3-vl:8b` tag is the *thinking* variant: it ignores `think:false` and returned empty JSON in testing.
* **OCR is not done by the VLM.** Apple Vision OCR is faster (under 1 s per page) and gives text that values can be grounded against.

## Setup

```bash
# Python 3.11+ (tested with 3.12)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Ollama (https://ollama.com), then:
ollama serve                         # if not already running as the app
ollama pull qwen3-vl:8b-instruct
```

OCR uses `ocrmac` (macOS Vision framework). On Linux, `pdf_text._ocr_page` is the single function to swap for Tesseract or PaddleOCR.

### Environment variables (all optional)

| Variable | Default |
|---|---|
| `LOCAL_MODEL_PROVIDER` | `ollama` |
| `LOCAL_MODEL_NAME` | `qwen3-vl:8b-instruct` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` |
| `LOCAL_MODEL_TIMEOUT` | `300` (seconds per call) |
| `LOCAL_MODEL_NUM_CTX` | `8192` |
| `TRADEPILOT_OCR_DPI` | `300` |
| `TRADEPILOT_CACHE_DIR` | `.tradepilot_cache` (OCR text and model responses; delete it to force a re-read) |
| `TRADEPILOT_OWN_COMPANY` | `VRV GLOBAL` (decides purchase vs sales side) |
| `TRADEPILOT_CHECKLIST` | `Documents/Basic Checklist.xlsx` |
| `TRADE_STORAGE_PATH` | `trade` (trade folders and `trade_register.xlsx`) |
| `ALLOWED_SENDER_DOMAIN` | `vrvglobal.com` (exact domain match) |
| `POLL_INTERVAL_SECONDS` | `30` |
| `MICROSOFT_CLIENT_ID`, `MICROSOFT_TENANT_ID` | Azure app registration (Outlook mode only) |
| `MICROSOFT_TOKEN_CACHE` | `.token_cache.json` (MSAL token cache; may point at an existing one) |
| `SQLITE_PATH` | `<TRADE_STORAGE_PATH>/tradepilot_state.db` |
| `MAX_PDF_MB` | `25` (larger PDF attachments are ignored) |

Put them in `.env` at the project root (`cp .env.example .env`). It is loaded automatically from the project root whatever the working directory, and it is git-ignored. Real environment variables take precedence; blank values fall back to the default. Relative paths (storage, token cache, checklist) are resolved from the project root. `LOG_LEVEL` (default `INFO`) sets the log level, and `-q` forces `WARNING`.

## Usage

```bash
.venv/bin/python tradepilot.py Documents/Metals/1          # one case
.venv/bin/python tradepilot.py Documents/Metals            # every case folder, independently
.venv/bin/python tradepilot.py Documents/Metals --as-of 2026-10-10 --out output -q
.venv/bin/python tradepilot.py Documents/Metals/1 --save-trade   # also store as a trade (TRD ref + register row)
.venv/bin/python tradepilot.py --poll-outlook                    # Outlook mode: poll the inbox until Ctrl+C
.venv/bin/python tradepilot.py --poll-outlook --once             # poll once and exit
.venv/bin/python -m pytest -q                              # tests (no Ollama needed)
```

The first run of a case takes about 3–5 minutes on an M2 Air (mostly model calls). Re-runs use the cache and take seconds.

### Outputs

```
output/<case>/result.json    full structured result: documents, fields with provenance, checks, evidence
output/<case>/report.md      sections A-G: document completeness, purchase validation, sales validation, physical
                             reconciliation, price validation, supporting documents, final outcome; then full evidence
output/<case>/email_response.txt   the reply text (in Outlook mode this exact text is sent in the thread)
output/<case>/report.xlsx    sheets: Checklist, Checks, Evidence, Extracted fields
output/summary.md|json|xlsx  batch table (pass/fail/review per case) and failed-check reasons
```

If Ollama is down, the model is missing, a call times out or the model returns invalid JSON, the affected documents and checks become REVIEW with the reason. The run still completes. A case that crashes is recorded as REVIEW with the error, and the batch continues.

## Outlook email workflow and trade storage

```
Outlook inbox (Graph, polled) ─┐
                               ├─> processor.process_trade(source/, trade folder) ─> reports + register (+ reply)
Local folder (--save-trade) ───┘        (the existing engine and report writer, unchanged)
```

**Sender policy.** Only mail whose From address has a domain of exactly `ALLOWED_SENDER_DOMAIN` is processed. The address is parsed, trimmed and lowercased; there is no substring or suffix matching, so `vrvglobal.com.fake.com`, `vrvglobal.co`, `mail.vrvglobal.com` and `vrvglobal.com@example.com` are rejected. If a message is sent on behalf of someone (Sender differs from From), both addresses must be allowed. A rejected message is recorded as `IGNORED_SENDER`: TradePilot does not download it, process it or reply, and never looks at it again.

**PDF only.** The decision is made from attachment metadata before anything is downloaded. Only non-inline file attachments with a PDF content type, or a generic type and a `.pdf` name, are accepted. Word, Excel, CSV, ZIP, images and inline logos are never downloaded. A downloaded file that does not start like a PDF (`%PDF-`) is discarded. Every accepted PDF is processed; there is no "exactly 5" rule, and missing mandatory documents are reported by the engine. An email with no PDFs creates no trade and gets no reply, which also prevents reply loops.

**Trade storage** (`TRADE_STORAGE_PATH`, default `trade/`):

```
trade/
  trade_register.xlsx            master register: one row per trade
  tradepilot_state.db            processed inbox messages (Outlook mode)
  TRD-2026-000001/
    source/                      the PDFs, sanitised names, never overwritten (PC.pdf, PC_2.pdf, ...)
    result.json  report.md  report.xlsx  email_response.txt
    trade_metadata.json          source, message ids, sender, subject, dates, attachments, status, reply status
```

The reference format is `TRD-<year>-<6-digit sequence>`. A reference is allocated under an exclusive file lock and claimed with an atomic `mkdir`, so two processes can never get the same one. The register is updated (upsert by Trade Ref) under a lock and saved to a temporary file, then atomically renamed. Existing rows, including hand-entered ones, are preserved.

**Duplicate protection.** SQLite records every message with `graph_message_id`, `trade_ref`, `sender`, `status`, `processed_at`, `replied_at`, `reply_status` and `error`. A message that is completed, ignored or already being replied to is never processed again. If processing was interrupted, the retry reuses the same `trade_ref`, so there is never a second folder or register row. The reply is never retried, so a message is never answered twice. Only mail received after the first poll is processed.

**The reply** is built deterministically from the structured result (the model never writes it). It goes in the same thread and is identical to `email_response.txt`:

```
TradePilot assessment completed.

Trade Ref: TRD-2026-000004
Status: GOOD TO GO | MISMATCH FOUND | REVIEW REQUIRED | MISMATCH / REVIEW REQUIRED

Mismatches:   every MISMATCH (and FAIL) item, one concise line each
Review:       every REVIEW item
Notes:        meaningful PASS_WITH_NOTE items (passes are summarised as a count, not listed)

TradePilot
```

**Failures.** If the engine fails, the folder and PDFs are kept, the metadata and register say `FAILED`, and the reply says "PROCESSING FAILED". It never says GOOD TO GO and contains no internal error text. A Graph error before the reply is retried on later polls, up to 3 attempts. A failed reply is recorded (`reply_status = failed`) and is not retried.

**Security.** Attachments are untrusted. Filenames are reduced to a safe base name (no directories or traversal), nothing is executed, and the email body is never read, so instructions in it cannot change any rule. Tokens stay in the MSAL cache and never go into metadata or logs. `trade/`, `.env`, `.token_cache.json` and `*.db` are git-ignored.

**Outlook setup** (delegated OAuth, the same app registration and token cache as before): set `MICROSOFT_CLIENT_ID` and `MICROSOFT_TENANT_ID`. Point `MICROSOFT_TOKEN_CACHE` at an existing cache, or sign in once with the device code shown on first start. The scopes are `Mail.ReadWrite` and `Mail.Send` on `/me`. `msal` and `httpx` are needed only for this mode.

## Known limitations

* **Missing document types.** Documents named by the checklist but absent from the dataset (proforma invoice, packing list, AIFTA certificate, quality report) are not classified. When a row requires them, `DOCS.REQUIRED` is REVIEW.
* **External data.** Debit notes, exposure limits, the payment ledger and bank-change approvals are not in the documents, so those items are always REVIEW.
* **Signature detection** comes from an 8B vision model. It is reliable for "present" but should not be read as proof of authenticity.
* **Partial shipments** are checked one shipment at a time. Cumulative quantity across shipments of the same contract (4/5, 6/7, 8/9) is not tracked.
* **Formula (LME) pricing** cannot be verified without the quotational-period fixation, so it is REVIEW.
* **Slow on CPU-only machines.** About 3 minutes per case on an M2 for the first run.
* **No ground truth in the repository**, so accuracy has been judged by manual inspection of the documents (see the batch results).

* **Outlook mode polls** (no webhooks), reads only the Inbox and handles messages received after the first start. Forwarded emails (`.eml`/item attachments) are not unpacked.
* **One email = one trade.** A follow-up email carrying corrected documents becomes a new trade; it is not merged into the earlier reference.

## Next steps

1. Get a few cases reviewed by Trade Operations to build ground truth, and add them as regression tests.
2. Connect the external data (debit notes, exposure limits, advance ledger, approved bank master) so the always-REVIEW items can be decided.
3. Add classifiers and schemas for proforma invoice, packing list and AIFTA certificate.
4. Track cumulative quantities and advances across shipments of the same contract.
5. Decide which checklist items block payment, and add that severity to the workbook rather than to code.
6. Add a non-macOS OCR backend for the on-prem server.
