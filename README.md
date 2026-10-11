# Ledgerline · Automated Three-Way Receiving Reconciliation

Automated month-end reconciliation engine built for Kestrel Industries. Compares handwritten receiving registers, ERP goods-received notes (GRN), and the storekeeper's spreadsheet to surface exceptions first and mathematically prove completeness with zero silent failures.

---

## 1. What It Does & How to Run It

### What It Does
- **Automated 3-Way Reconciliation:** Ingests all four monthly files once (2 register page photos, 1 ERP CSV export, 1 storekeeper Excel sheet).
- **AI-Powered OCR:** An AI model (`gpt-5.6-luna`) reads *only* the handwritten register pages twice independently for OCR verification; ERP exports and store sheets are parsed strictly deterministically without leaving the system.
- **Exceptions First:** Lines that agree are cleared automatically. Discrepancies (mismatched quantities, dates, sequence gaps, or struck-out handwritten values) are surfaced side-by-side in a human review queue.
- **Completeness Proof:** Mathematically verifies that every row across all three sources lands in exactly one outcome, tying totals back to input counts (86 of 86 rows accounted for).
- **Director Messaging:** Once finalised, a concise executive summary and `report.html` are dispatched to Telegram (designed to port directly to WhatsApp Cloud API), alerting directors without requiring a dashboard login.

---

### How to Run

#### Hosted Version
1. Open the deployed application URL.
2. Sign in with:
   - **Email:** `demo@gmail.com`
   - **Password:** *(Provided in the submission email)*
3. Navigate to **Start a New Run** (`/runs/new`), upload the four sample files from `data/kestrel-2026-09/`, and start reconciliation.
4. Review exceptions under **Review**, inspect the mathematical proof under **Proof**, and click **Finalise**.

#### Local Setup (Windows / macOS / Linux)
1. **Prerequisites:** Python 3.12, [`uv`](https://docs.astral.sh/uv/), Node 20+, and `pnpm`.
2. **Backend Setup:**
   ```bash
   cd apps/api
   uv sync
   uv run uvicorn app.main:app --reload --port 8000
   ```
3. **Frontend Setup:**
   ```bash
   cd apps/web
   pnpm install
   pnpm dev
   ```
4. **Access Web App:** Open `http://localhost:3000` in your browser.
5. **Offline Test Suite:** Run the offline verification test suite (under 30 seconds, no external keys needed):
   ```bash
   cd apps/api && uv run pytest -m "not integration and not live"
   ```

---

## 2. Assumptions Made

- **Match Key:** The GRN number serves as the primary match key across all three sources.
- **Physical Truth vs System Truth:** While the handwritten register reflects physical receipt, no single source is treated as infallible truth. When sources disagree, the outlier is highlighted and presented to a person for decision.
- **Unit Conversions:** Exact unit conversions (e.g., yards to metres for fabric/textile goods) count as matches and are logged in normalisation audit trails.
- **Returns & Voided Items:** Supplier returns are categorized as non-receipts rather than reconciled against debit notes. Missing GRN numbers are reported as sequence gaps for audit acknowledgment.
- **Struck-Out Handwritten Entries:** Struck-out numbers with replacement annotations in the physical register require human confirmation rather than automated guesswork.
- **Control Checks:** ERP entries posted after month-end or keyed by unauthorized personnel are flagged as control exceptions.

---

## 3. How It Fails and How Someone Would Know

Ledgerline is designed to fail safely, fail loudly, and never produce silent corruption or partial results:

- **Malformed or Corrupted Files:** If any uploaded file has invalid headers, empty rows, or corrupted images, validation fails before any AI model call is made, returning a detailed list of syntax and structural errors.
- **AI Extraction Downtime / Timeout:** 3 retries with exponential backoff. If extraction fails, the step is marked failed, and an incident alert is dispatched to the engineering ops channel. Cached pages are preserved so re-running only processes failed pages.
- **Failing Completeness Proof:** If any single row cannot be accounted for, the system halts finalisation, withholds the summary, and alerts ops with exact row discrepancies.
- **Telegram / Messaging Failure:** If Telegram is unreachable, the run is still safely finalised and locked, the notification status is flagged as `FAILED`, and a prominent *"Summary not delivered — Resend"* banner is displayed in the UI.
- **Duplicate Send Prevention (At-Most-Once):** Finalising twice or experiencing a network drop mid-send never sends a duplicate message to the director.
- **Demonstrating Failures On-Demand:** With `ALLOW_FAULTS=true` enabled, append `?fault=` with `EXTRACT_TIMEOUT`, `EXTRACT_BAD_JSON`, `TELEGRAM_DOWN`, or `PROOF_DROP_ROW` to demonstrate each failure recovery mode live.

---

## 4. How the Data Was Handled

- **Data Privacy & Minimal Exposure:** Only the handwritten register photos are processed by OpenAI for extraction. ERP exports and storekeeper sheets remain strictly within local/private storage and never leave the system.
- **Audit Trails & Lineage:** Every normalized value preserves its raw original string alongside it in an immutable normalisation log.
- **Signed URL Access:** Images and generated reports reside in private storage buckets, accessible only via temporary signed URLs with strict expiry (10-minute upload, 5-minute download).
- **Data Integrity:** The audit log cannot be modified or deleted through the application interface or API.

---

## 5. What I'd Do With Another Week

- **WhatsApp Cloud API Integration:** Implement direct inbound webhook support for WhatsApp Cloud API so warehouse staff can photo-message register pages directly to a WhatsApp business number to trigger runs automatically.
- **Bounding-Box Visual Verification:** Display interactive cell-level crops directly over the physical photograph in the review queue to speed up human confirmation.
- **Self-Learning Aliases:** Enable the engine to learn verified supplier abbreviations and item unit aliases from human reviewer choices over time.
- **Direct ERP Write-Back:** Build automated write-back connectors to update approved corrections back into the host ERP system upon finalisation.
- **Multi-Tenant Retention Policies:** Add automated retention windows and cryptographic data purging per company policy.

---

## 6. AI Tools Used and for What

- **Claude / Spec-Driven Development:** Architecture design, specification drafting, and test-first scaffold planning.
- **Google Antigravity / Gemini 3.8:** End-to-end fullstack implementation, test execution, environment configuration, and verification.
- **GPT-5.6 Luna (Embedded Product AI):** Embedded directly inside the product pipeline to read handwritten register pages twice independently for OCR extraction.
