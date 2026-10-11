# Time Log · Nocturnal AI Take-Home Assessment

**Candidate:** AI Automation Engineer Candidate  
**Project:** Task 4 · Register Reconciliation (Ledgerline)  
**Total Time Spent:** ~7.0 Focused Hours (Budget: 6–8 hours)  

---

## Breakdown by Part

| Part / Phase | Description | Hours Spent |
| :--- | :--- | :---: |
| **1. Architecture & Specification** | Reviewing requirements, analyzing sample files (`data/kestrel-2026-09/`), drafting data contracts, and planning the 3-way reconciliation pipeline. | 1.0 hr |
| **2. AI Extraction Pipeline** | Building the prompt and vision schema for reading handwritten register photos twice independently, handling struck-out values, and validating OCR responses. | 1.5 hrs |
| **3. Deterministic Ingestion & Normalisation** | Parsing ERP CSV exports and Storekeeper Excel spreadsheets; standardizing dates, units (yards to metres conversion), and building match keys. | 1.0 hr |
| **4. 3-Way Reconciliation & Completeness Proof** | Implementing core matching logic (matched, field-level mismatches, missing items) and writing the mathematical completeness verification engine (86/86 rows). | 1.5 hrs |
| **5. Frontend UI & Review Workflow** | Building the Next.js review interface, exception tables, proof dashboard, and authentication screens. | 1.0 hr |
| **6. Messaging & Telegram Dispatch** | Implementing executive summary builder, at-most-once delivery, retry with backoff, and Ops error alert dispatching. | 0.5 hr |
| **7. Testing, Fault Injection & Documentation** | Running offline test suites, testing fault-injection modes (`?fault=`), drafting README, and recording demo walk-through. | 0.5 hr |
| **Total** | | **7.0 hrs** |
