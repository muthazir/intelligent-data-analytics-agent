# IDAA — Intelligent Data Analytics Agent

A database-backed web application that turns a business CSV (ecommerce orders,
cafe sales or gym memberships) into validated data in **Microsoft SQL Server**,
deterministic analytics, anomaly detection, a narrated summary and a PDF report.

```
Browser ──► FastAPI /upload ──► normalize · map synonyms · detect schema
        ──► pre-clean · validate · clean ──► SQL Server (IDAA_DB)
        ──► /analyze: analytics + Z-score anomalies (pandas)
        ──► narration: Gemini (optional) or deterministic fallback
        ──► AnalysisRun + Anomaly + Insight + result_json (one transaction)
        ──► dashboard (same API) · /report → PDF (from the saved result)
```

**Python owns every number.** Gemini only rewrites the computed facts as
sentences; any sentence containing a number that is not in the facts is
discarded. Without a Gemini key the app works fully and uses deterministic,
fact-based narration (labelled as such in the UI and the PDF).

---

## 1. Prerequisites

- Windows 10/11 with **Python 3.11+** (developed with Python 3.13)
- **Microsoft SQL Server** (Express/Developer) + SSMS, and **ODBC Driver 17 or 18 for SQL Server**
- A modern browser (Edge/Chrome). Internet access for the Chart.js, Lucide icon and Geist font CDNs.
- Optional: a Google Gemini API key

## 2. Database setup (SQL Server)

The application database is `IDAA_DB`.

**New installation** (empty server), in SSMS or with `sqlcmd`:

```cmd
sqlcmd -S localhost -E -C -b -i sql\schema.sql
sqlcmd -S localhost -E -C -b -d IDAA_DB -i sql\migrations\001_dataset_scoped_entities_and_result_json.sql
```

> ⚠️ `sql/schema.sql` **drops and recreates** `IDAA_DB`. Never run it against a
> database that holds data you want to keep.

**Existing database** (created by an older version): run only the migration
(it is idempotent and transactional — safe to re-run):

```cmd
sqlcmd -S localhost -E -C -b -d IDAA_DB -i sql\migrations\001_dataset_scoped_entities_and_result_json.sql
```

Take a backup first (`BACKUP DATABASE IDAA_DB TO DISK='...' WITH COPY_ONLY, CHECKSUM`).
`sql/migrations/001_rollback.sql` undoes the migration (see its header for limitations).

### Tables

| Table | Purpose |
|---|---|
| `Dataset` | one row per upload (ecommerce, cafe and gym) |
| `DatasetCustomer`, `DatasetProduct` | customers / products **scoped to one dataset** — the same `P001` in two uploads never mixes |
| `EcommerceOrder` | ecommerce order rows; composite FKs `(dataset_id, customer_id/product_id)` keep them inside their dataset |
| `AnalysisRun` | one row per `/analyze`: KPIs, `narration_source` (`gemini`/`fallback`) and the full `result_json` |
| `Anomaly`, `Insight` | anomaly and narration rows of each run |
| `Customer`, `Product` | legacy (pre-migration) global tables, kept for history, no longer written |
| `SchemaVersion` | applied migrations |

Cafe and gym uploads store their `Dataset` row and all analysis runs in SQL
Server; their row-level data stays in the cleaned CSV under `uploads/`.

## 3. Installation

```cmd
cd "INTELLIGENT-DATA-ANALYTICS-AGENT-main"
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env
```

Edit `.env`:

```
DATABASE_URL=mssql+pyodbc://localhost\SQLEXPRESS/IDAA_DB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
GEMINI_API_KEY=            # optional
# GEMINI_MODEL=            # optional model override
```

Use your own server name (e.g. `localhost` for a default instance). If
`DATABASE_URL` is empty the app still runs in **file-only mode** (no SQL Server).
`.env` is git-ignored — never commit it.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | no (recommended) | SQLAlchemy URL of `IDAA_DB`. Empty → file-only mode. |
| `GEMINI_API_KEY` | no | Enables Gemini narration. Empty → deterministic narration. |
| `GEMINI_MODEL` | no | Overrides the Gemini model ID (default `gemini-3.5-flash-lite`). |
| `IDAA_TEST_DATABASE_URL` | tests only | Runs the test suite against a SQL Server **test** copy instead of SQLite. |
| `IDAA_E2E` | tests only | `1` enables the headless-browser end-to-end test. |

## 4. Run

```cmd
.venv\Scripts\python -m uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/** — the dashboard is served by the API itself.
(`idaa_fixed.html` can also be opened directly; it then calls `http://127.0.0.1:8000`.)
API docs: http://127.0.0.1:8000/docs. The sidebar shows live status from `/health`
(API, SQL Server connection, Gemini configured or not).

## 5. Using the application

1. **Upload & analyse** — drop a CSV, pick *Auto-detect* (default) or a dataset type.
   The pipeline timeline shows each real step (upload progress → detect/validate/clean →
   store → analytics → narrate & save). Afterwards the page shows the detected schema
   with per-schema coverage, column renames, what pre-cleaning fixed and validation
   notes; failures show the exact errors and a suggestion. On success the app moves to
   the dashboard.
2. **Dashboard** — dataset identity, a signal strip (trend or leading segment,
   anomalies, narration source, storage), one dominant metric with supporting KPIs,
   two schema-specific charts, top performers, anomalies and the first narrated insight.
3. **Insights** — facts → anomalies → narration lineage; the narration (violet for
   Gemini, cyan for the deterministic fallback) with every number highlighted; the
   computed facts; and an expandable anomaly ledger with |z| severity.
4. **PDF report** — a preview mirroring the PDF's sections, generation state and
   **Download PDF** (built on the server from the saved analysis, never recalculated).

A reload restores the last analysis (via `GET /results/{file_id}`). The layout adapts
from phones (bottom navigation) to wide desktops, supports keyboard navigation and
honours *reduced motion*.

### Supported datasets

| Schema | Required columns | Optional columns |
|---|---|---|
| `ecommerce` | order_id, order_date, order_status, customer_id, product_id, product_name, category, unit_price, quantity, total_amount, payment_method | customer_email, discount, shipping_city, shipping_state, return_requested |
| `cafe` | order_id, order_date, item_name, category, price, quantity, payment_method, store_location | customer_id |
| `gym` | member_id, member_name, gender, age, membership_type, membership_start_date, membership_end_date, monthly_fee, payment_status | trainer_assigned, height_cm, weight_kg, attendance_days, fitness_goal |

The definitions (types, ranges, allowed values, aliases) live in `app/schema/`.
A file needs at least 5 data rows and at most 10 MB.

**Auto-detection** (`schema_type=auto`, the UI default): after header normalisation and
alias mapping, every schema is scored by how many of its required columns are present.
A schema is chosen only when **all** its required columns are found and it is the
single best match; otherwise the upload is rejected with 422 and the per-schema
coverage. Choosing a type manually skips detection and validates against that schema.

### Flexible CSV input

- Column order, case, spacing, `camelCase`, `kebab-case`, dots — all accepted
  (`OrderID`, `Order Date`, `order-status` → `order_id`, `order_date`, `order_status`).
- Common synonyms are mapped (e.g. `Qty`→`quantity`, `Order Total`/`Amount`→`total_amount`,
  `MRP`/`Price`→`unit_price`, `Outlet`→`store_location`, `Sex`→`gender`, `Plan`→`membership_type`);
  the lists live in each column's `aliases` in `app/schema/*_schema.py`.
- Light fixes before validation: `₹1,299.00`/`Rs. 450` → numbers, `Credit Card`→`credit_card`,
  `Y`/`TRUE`→`yes`, exact duplicate rows removed. Blank optional values stay blank
  (nothing is invented).
- Unsupported files are rejected with **422** and per-schema coverage
  ("closest match: ecommerce, missing total_amount"); a wrong manual choice gets a
  **400** with the exact errors and a suggestion ("looks like a gym dataset").

## 6. API

| Method & path | Purpose |
|---|---|
| `POST /upload?schema_type=auto\|ecommerce\|cafe\|gym` | validate, clean and store a CSV (default `cafe` for backwards compatibility; the UI sends `auto`) |
| `POST /analyze/{file_id}` | analytics, anomalies, narration; saves an `AnalysisRun` |
| `GET /results/{file_id}` | latest saved result (`result_source`: `database` or legacy `file`) |
| `GET /report/{file_id}` | PDF built from the saved result |
| `GET /dataset/{file_id}` | dataset metadata (+ SQL row preview for ecommerce) |
| `GET /anomalies/{file_id}`, `GET /insights/{file_id}` | rows of the latest run |
| `GET /health` | status booleans (no configuration values) |
| `GET /` | the dashboard |

## 7. Gemini

Set `GEMINI_API_KEY` in `.env` and restart. The client uses the `google-genai`
SDK with a 20 s timeout; the model defaults to `gemini-3.5-flash-lite` and can be
overridden with `GEMINI_MODEL` (model names change over time — see
https://ai.google.dev/gemini-api/docs/models). If the key is missing, the call
fails, times out, or the answer contains numbers that are not in the facts, the
deterministic narration is used and `narration_source = "fallback"`.

### PDF report

`GET /report/{file_id}` builds the PDF on the server with ReportLab from the **saved**
analysis result (nothing is recalculated): dataset and persistence details, the
schema's key metrics, up to two breakdown charts, anomalies (Z-score > 2.5) and the
narration with its source (Gemini or deterministic). Text is escaped, and a Unicode
font (Arial, Segoe UI or DejaVu Sans, whichever is installed) is embedded so `₹` renders;
otherwise Helvetica is used. The file is cached in `uploads/` per analysis run and
rebuilt when the report format version changes.

## 8. Tests

```cmd
.venv\Scripts\python -m pytest tests
```

- Default: a temporary SQLite database (foreign keys on) — SQL Server is not touched.
- Against a SQL Server **test** database: `set IDAA_TEST_DATABASE_URL=<url of a copy, e.g. IDAA_DB_MIGTEST>`.
  The suite refuses to run against `IDAA_DB`. It writes test rows into that database.
- No test calls the real Gemini API (clients are stubbed).

**Browser end-to-end test** (headless Microsoft Edge or Google Chrome over the DevTools
protocol; no extra packages): `set IDAA_E2E=1` and run pytest. It starts its own
server on a temporary SQLite database and drives the real UI: auto-detect and manual
uploads, column mapping, KPIs and charts, HTML-injection safety, PDF download,
navigation, 390 px and 1280 px layouts, stale-data reset between datasets, error
panels and reload restore. It can also be pointed at a running server:

```cmd
.venv\Scripts\python tests\e2e\browser_e2e.py --base-url http://127.0.0.1:8000 --out <folder>
```

That mode writes real datasets into whatever database the server uses and prints
the created `dataset_ids`, so they can be removed afterwards.

## 9. Project layout

```
app/
  main.py               routers, GET / (dashboard), GET /health
  routes/               upload, analyze (+results), dataset (+anomalies, insights), report
  services/             normalization, column_mapping, schema_detection, pre_clean,
                        schema_validation, structural_validation, cleaning, analytics,
                        anomaly, insight_builder, ai_narration, result_store, dtypes
  schema/               ecommerce / cafe / gym column rules (+ aliases), SCHEMA_REGISTRY
  db/                   SQLAlchemy models, session, crud
sql/schema.sql          bootstrap DDL (destructive) · sql/migrations/  additive migrations
idaa_fixed.html         single-page dashboard (HTML/CSS/JS + Chart.js)
tests/                  pytest suite · tests/e2e/browser_e2e.py  browser harness
sample_data/            ecommerce_sample.csv
```

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| System status shows *SQL Server: Unreachable* | Check the SQL Server service, the server name and ODBC driver in `DATABASE_URL`. |
| `Invalid object name 'dbo.DatasetCustomer'` | Run migration 001 (section 2). |
| Upload 422 "does not match any supported dataset" | Check the listed missing columns, or rename them; see the aliases in `app/schema/`. |
| Narration always "deterministic fallback" | `GEMINI_API_KEY` missing/invalid, or Gemini unreachable — the dashboard explains which. |
| Charts missing, text otherwise fine | The Chart.js CDN is blocked/offline. |
| `/results` or `/report` 404 for an old dataset | It was analysed before migration 001 and its local files are gone — run `/analyze` again. |

## 11. Known limitations

- Cafe/gym row-level data is not stored in SQL Server tables (by design so far).
- Ambiguous dates such as `03/04/2024` are parsed month-first.
- Historical rows ingested by versions before 1.1 may contain imputed optional values
  (e.g. a blank e-mail filled with another customer's); they were intentionally left unchanged.
- Gemini integration is verified with stubs; a live call requires your own key.
- Runs locally: there is no user authentication and no hosted deployment yet.
