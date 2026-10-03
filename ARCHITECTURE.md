# IDAA — Architecture Blueprint

## 1. System at a Glance

IDAA is a database-backed analytics web application.

```text
┌─────────────────────────────────────────────────────────────────────┐
│                         RESPONSIVE WEB UI                            │
│  Schema Select → CSV Upload → Progress → Dashboard → PDF Report     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ HTTP / JSON / multipart / PDF
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                             FASTAPI                                 │
│ Upload │ Dataset │ Analyze │ Anomalies │ Insights │ Report           │
└───────────────┬──────────────────────────────┬──────────────────────┘
                │                              │
                ▼                              ▼
┌──────────────────────────────┐   ┌────────────────────────────────┐
│     DETERMINISTIC ENGINE      │   │        AI NARRATION             │
│ validation                    │   │ structured facts               │
│ normalization                 │   │       ↓                         │
│ cleaning                      │   │ Google Gemini                   │
│ analytics                     │   │       ↓                         │
│ anomaly detection             │   │ concise business narration      │
│ insight building              │   └────────────────────────────────┘
└───────────────┬──────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      SQL SERVER — IDAA_DB                            │
│ Dataset / Customer / Product / EcommerceOrder                        │
│ AnalysisRun / Anomaly / Insight                                     │
└─────────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  CANONICAL ANALYSIS RESULT                           │
│     persisted result used by dashboard/report where applicable      │
└───────────────────────────────┬─────────────────────────────────────┘
                                ▼
                         ReportLab PDF
```

---

## 2. Architectural Boundary: Facts vs Narration

This is the most important design rule in IDAA.

### Deterministic layer
Python is responsible for:
- parsing
- validation
- cleaning
- normalization
- aggregation
- calculations
- anomaly detection
- structured insight generation
- persistence

These are testable and reproducible.

### AI layer
Gemini is responsible for:
- turning structured facts into readable language
- summarizing patterns
- producing concise recommendations when supported by the facts

Gemini is not responsible for discovering the numerical truth from scratch.

```text
            SOURCE DATA
                │
                ▼
        Python calculations
                │
                ▼
       structured insight JSON
                │
                ▼
              Gemini
                │
                ▼
        narrative sentences
```

If Gemini fails, the first half of the architecture should continue to work.

---

## 3. End-to-End Request Lifecycles

### 3.1 Upload lifecycle

```text
Browser
  │
  │ POST /upload?schema_type=ecommerce
  │ multipart CSV
  ▼
FastAPI upload route
  │
  ├── structural validation (raw file)
  ├── normalization (column names incl. camelCase/hyphens, values)
  ├── schema resolution (manual, or auto-detection → 422 if unsupported)
  ├── column mapping (per-column aliases / synonyms)
  ├── pre-validation cleaning (currency/thousands, categorical spelling, exact duplicates)
  ├── schema validation (structured 400 detail + "looks like X" suggestion)
  ├── cleaning
  │
  ▼
SQL persistence
  │
  ├── Dataset
  ├── Customer/Product where applicable
  └── domain rows such as EcommerceOrder
  │
  ▼
response
  ├── file_id
  ├── row count
  ├── warnings
  ├── cleaning summary
  └── db persistence status
```

### 3.2 Analysis lifecycle

```text
Browser
  │
  │ POST /analyze/{file_id}
  ▼
Analysis route
  │
  ├── fetch persisted dataset/data
  ├── calculate KPIs
  ├── generate domain breakdowns
  ├── detect anomalies
  ├── build structured insights
  ├── call Gemini if available
  ├── create AnalysisRun
  ├── persist Anomaly rows
  └── persist Insight rows
  │
  ▼
Analysis response
```

### 3.3 Dashboard lifecycle

```text
Analysis API response
        │
        ├── KPI data
        ├── chart data
        ├── anomalies
        └── narration
        │
        ▼
Frontend state
        │
        ▼
DOM + Chart.js
```

The UI should not independently calculate a different total revenue or order count unless that calculation is purely presentational and guaranteed to match the backend contract.

### 3.4 Report lifecycle

```text
GET /report/{file_id}
        │
        ▼
load canonical analysis result
        │
        ▼
build report
        │
        ├── metadata
        ├── KPI table
        ├── breakdown/chart
        ├── anomalies
        └── narration
        │
        ▼
ReportLab
        │
        ▼
application/pdf
```

The report is a presentation of analysis results, not a second analytics engine.

---

## 4. Backend Layer Map

```text
app/main.py
    │
    ├── router registration
    │
    └── application entrypoint

app/routes/
    │
    ├── upload.py       → ingestion workflow
    ├── analyze.py      → analysis orchestration
    ├── dataset.py      → retrieval endpoints
    └── report.py       → PDF generation

app/services/
    │
    ├── structural_validation.py
    ├── normalization.py
    ├── cleaning.py
    ├── schema_validation.py
    ├── analytics.py
    ├── anomaly.py
    ├── insight_builder.py
    └── ai_narration.py

app/db/
    │
    ├── models.py       → SQLAlchemy table model definitions
    ├── session.py      → DB engine/session
    └── crud.py         → persistence/retrieval operations

app/schema/
    │
    ├── ecommerce_schema.py
    ├── cafe_schema.py
    └── gym_schema.py
```

A clean responsibility boundary is:

```text
routes → orchestration
services → business logic
schema → domain validation/shape
DB layer → persistence
frontend → presentation
```

Do not move substantial business logic into HTML/JavaScript simply to reduce backend work.

---

## 5. SQL Server ER Model

```text
                       ┌──────────────┐
                       │   Dataset    │
                       │ PK dataset_id│
                       │ UK file_id   │
                       └──────┬───────┘
                              │ 1:N
            ┌─────────────────┼──────────────────┐
            │                 │                  │
            ▼                 ▼                  ▼
   ┌────────────────┐  ┌───────────────┐  ┌──────────────┐
   │EcommerceOrder  │  │ AnalysisRun   │  │Domain data   │
   │PK order_row_id │  │PK run_id      │  │may vary by   │
   │FK dataset_id   │  │FK dataset_id  │  │schema        │
   └───┬───────┬────┘  └──────┬────────┘  └──────────────┘
       │       │               │ 1:N
       │       │               ├──────────────┐
       │       │               ▼              ▼
       │       │       ┌──────────────┐ ┌──────────────┐
       │       │       │   Anomaly    │ │   Insight    │
       │       │       │ PK anomaly_id│ │PK insight_id │
       │       │       │ FK run_id    │ │FK run_id     │
       │       │       └──────────────┘ └──────────────┘
       │       │
       ▼       ▼
  Customer   Product
  1:N        1:N
```

### Migration 001 (implemented)

Customers and products are weak entities of `Dataset`: `DatasetCustomer (dataset_id, customer_id)`
and `DatasetProduct (dataset_id, product_id)`. `EcommerceOrder` references them through
composite foreign keys, so an order can only point at a customer/product of the same dataset
and two uploads that reuse an id (e.g. `P001`) never share names, categories or e-mails.
The legacy global `Customer`/`Product` tables are kept unchanged and are no longer written.
`AnalysisRun.result_json` holds the canonical analysis result (read by `/results` and `/report`
before any local file), and `AnalysisRun.narration_source` records `gemini` or `fallback`.

### Important interpretation

`AnalysisRun` is the bridge between a dataset and a specific analytical execution.

That allows multiple analyses of the same dataset without mixing anomalies/insights between runs.

---

## 6. Why AnalysisRun Matters

Without an analysis-run entity:
- repeated analyses can overwrite context
- anomalies become difficult to associate with a specific execution
- insights can be mixed across runs

With `AnalysisRun`:

```text
Dataset 3
  │
  ├── Run 2
  │     ├── anomalies
  │     └── insights
  │
  └── Run 3
        ├── anomalies
        └── insights
```

This is especially important for testing dataset isolation.

---

## 7. Data Ownership Rules

### SQL Server owns
- ingested domain data
- dataset metadata
- customers/products where applicable
- analysis run metadata
- anomaly records
- insight records

### Backend calculation layer owns
- temporary DataFrame transformations
- deterministic KPI calculations
- intermediate analytical structures

### AI layer owns
- narrative wording only

### Frontend owns
- presentation state
- loading/error UI state
- chart rendering
- user interaction

No layer should silently assume ownership of another layer's source of truth.

---

## 8. Schema-Aware UI Strategy

The dashboard should conceptually render through a schema adapter:

```text
analysis response
      │
      ▼
identify schema_type
      │
      ├── ecommerce → ecommerce KPI/chart configuration
      ├── cafe      → cafe KPI/chart configuration
      └── gym       → gym KPI/chart configuration
```

This prevents common failures such as:
- ecommerce KPI cards being displayed for gym data
- cafe totals becoming zero because the UI expects a field that cafe analytics never returns
- fake “unique customers” values for a domain where that metric does not exist

The implementation can stay simple. A full frontend framework is not required if the existing HTML/JS is adequate.

---

## 9. API Contract Philosophy

Every frontend-visible field should have a clear origin.

Example:

```text
KPI card: Total Orders
       ↑
analysis.total_orders
       ↑
analytics service
       ↑
rows persisted for file_id
       ↑
SQL Server
       ↑
uploaded CSV
```

This chain should be explainable during the viva.

Avoid fields whose origin cannot be explained.

When an API contract changes:
1. update backend
2. update frontend
3. update report logic if affected
4. update tests
5. update README/API documentation

---

## 10. Error Handling Architecture

The application should fail at the narrowest sensible layer.

### Upload failures
Examples:
- unsupported file type
- missing required columns
- malformed data
- schema mismatch

Return a clear HTTP error with a useful user-facing message.

### Analysis failures
Examples:
- file ID not found
- missing persisted data
- invalid domain state

Do not return a misleading successful dashboard.

### AI failures
Gemini failures should degrade to fallback narration where possible.

### Report failures
Report generation should return a clear error rather than an empty or corrupt PDF.

### Frontend failures
Show:
- what failed
- whether the user can retry
- whether previously loaded data remains available

Avoid raw stack traces in the user interface.

---

## 11. AI Failure State Machine

```text
                 ┌───────────────┐
                 │ insight data  │
                 └───────┬───────┘
                         ▼
                GEMINI_API_KEY?
                    /        \
                  no          yes
                  │            │
                  ▼            ▼
             fallback     call Gemini
                               │
                         ┌─────┴─────┐
                         │ success?  │
                         └─────┬─────┘
                           yes / \ no
                              /   \
                             ▼     ▼
                        narration fallback
```

The user should still be able to demonstrate the dashboard and report even without an API key.

---

## 12. Report Safety Rules

ReportLab interprets some text content as markup when using paragraph-style elements.

Therefore free text from:
- filenames
- dataset descriptions
- AI narration
- category names
- user-provided values

must be escaped/sanitized before insertion into markup-aware PDF components.

The report generator should support ordinary characters such as:

```text
&  <  >  '  "
```

and should not lose or reinterpret user content.

Unicode also needs deliberate handling. Do not assume the base ReportLab font supports every glyph.

---

## 13. Performance Philosophy

This is an MCA academic application, not a high-scale production platform.

Prefer:
- readable code
- deterministic results
- clear API contracts
- simple SQL queries
- straightforward DataFrame operations
- sensible file sizes

Do not introduce distributed systems or complex caching unless demonstrated necessity exists.

---

## 14. Testing Pyramid

### Level 1 — Syntax/import

```text
python -m compileall app
```

### Level 2 — Service tests

Check:
- cleaning
- normalization
- analytics
- anomaly detection
- AI fallback/parser

### Level 3 — API tests

Check:
- upload
- analyze
- dataset retrieval
- anomalies
- insights
- report

### Level 4 — Integration

Check:

```text
CSV → SQL Server → analysis → persistence → API
```

### Level 5 — Browser acceptance

Check:

```text
upload → dashboard → insights → anomalies → PDF
```

### Level 6 — Isolation

Upload dataset A and dataset B.

Confirm that:
- A results belong to A
- B results belong to B
- report A contains A data
- report B contains B data
- insights/anomalies are not mixed

---

## 15. Acceptance Test Script

The final application should support a repeatable manual test:

### Test A — Ecommerce
1. Start SQL Server.
2. Start FastAPI.
3. Open the frontend.
4. Select Ecommerce.
5. Upload sample ecommerce CSV.
6. Confirm upload succeeds.
7. Confirm SQL Server `Dataset` and order/customer/product records update.
8. Run analysis.
9. Confirm `AnalysisRun` appears.
10. Confirm anomaly/insight rows appear.
11. Confirm dashboard KPIs match API response.
12. Confirm chart is populated.
13. Confirm AI narration or fallback appears.
14. Download PDF.
15. Inspect PDF for correct dataset information.

### Test B — Second dataset
Repeat with a different dataset.

The two datasets must remain isolated.

### Test C — Gemini missing
Remove/blank the API key temporarily.

Expected:
- upload works
- analysis works
- fallback narration appears
- report still works

### Test D — Bad upload
Provide a schema-mismatched or structurally invalid CSV.

Expected:
- useful error
- no corrupted partial dashboard
- no misleading success state

---

## 16. Change-Control Rules

Before a large change, answer:

```text
What exact requirement does this change satisfy?
Which files are required?
What existing behaviour could it break?
How will it be tested?
```

Then implement.

After implementation:

```text
inspect diff
→ compile/test
→ run app
→ exercise affected endpoint
→ check frontend
→ verify DB if relevant
```

Avoid “while I was here” refactors.

---

## 17. Do Not Build These Unless Required

Avoid adding:
- a second backend framework
- a second database
- native mobile app
- microservices
- WebSockets
- Redis
- Kafka
- vector DB
- authentication/roles
- cloud deployment infrastructure
- Docker/Kubernetes
- LLM agents that independently manipulate SQL

These may sound sophisticated but would increase risk without being required for the stated academic objective.

---

## 18. Future Extension Points

The design can later support:
- more business schemas
- scheduled analysis runs
- user authentication
- cloud SQL Server
- richer PDF layouts
- historical dashboard views
- model-based forecasting
- anomaly explanation

But those are extension points, not prerequisites for the current project.

---

## 19. Mental Model for the Student

The entire project can be explained in six questions:

### Q1. Where does data enter?
Frontend upload → FastAPI.

### Q2. Where is data stored?
SQL Server.

### Q3. Who calculates the numbers?
Python analytics services.

### Q4. Who detects unusual values?
The anomaly detection service.

### Q5. What does Gemini do?
Turns structured analytical facts into readable business narration.

### Q6. How does the professor see the result?
Dynamic dashboard + generated PDF report.

That mental model should remain true even if implementation details evolve.
