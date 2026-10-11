# Time Log · Nocturnal AI Take-Home Assessment

**Candidate:** Mutahir Hussain Shah  
**Project:** Task 4 · Register Reconciliation (Ledgerline)  
**Total:** about 50 focused hours over 4 days (6–11 October 2026)  

This is well over the 6–8 hour guide. I chose to build the full flow end to end (AI reads, a
deterministic engine, review, proof, Telegram, a web app) and deploy it, rather than stop at a
script. Hours are rounded and taken from the commit history; each phase was merged as its own
branch (`001-foundation` to `007-ship`).

---

## Breakdown by part

| Part | What it covered | When | Hours |
| :--- | :--- | :--- | :---: |
| **Architecture & specs** | Reading the brief and the pack, finding the data problems, blueprint, per-phase specs, plans and tasks | before and alongside each phase | ~4 |
| **1. Foundation** (`001`) | Project setup, domain model and enums, scenario clock, CI, secrets scanning | 6 Oct | ~4 |
| **2. Core engine** (`002`) | Parsing ERP and sheet, normalisation (dates, units, suppliers), three-way matching, control checks, completeness proof | 6–7 Oct | ~4 |
| **3. AI extraction** (`003`) | Two independent reads per page, merge and doubt rules, struck-out and illegible cells, page cache | 7 Oct | ~5 |
| **4. API & storage** (`004`) | FastAPI routes, Supabase schema and guards, private storage, signed links | 7 Oct | ~5 |
| **5. Review & delivery** (`005`) | Review queue, finalise, report.html, Telegram summary with at-most-once send, ops alerts, fault injection | 7–9 Oct | ~8 |
| **6. Web app** (`006`) | Next.js screens: runs, start a run, overview, results, review, proof, audit | 9–10 Oct | ~10 |
| **7. Ship** (`007`) | Production deploy, security and isolation tests, NFR report, README, redesign, production QA and fixes | 10–11 Oct | ~10 |
| **Total** | | | **~50** |

## What I'd cut to fit 8 hours

The core the brief asks for is parts 2, 3 and the summary in 5: the engine, the AI reads with the
doubt rules, the proof and the Telegram message (~17 hours). The web app, deployment and redesign
are what took it past the guide.
