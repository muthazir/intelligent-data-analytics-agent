/* ============================================================================
   IDAA — Phase 1 Database Foundation
   Microsoft SQL Server / T-SQL DDL script
   ----------------------------------------------------------------------------
   Run this ENTIRE script in SSMS (SQL Server Management Studio) against a
   server instance you can connect to. It is idempotent-ish: it drops the
   database if it already exists so you can re-run it safely while developing.

   Primary Phase-1 dataset: ECOMMERCE
   (cafe/gym schemas remain file-based in Phase 1 — see PROJECT LOGS / README)

   Entities:
       DATASET          — one row per uploaded CSV (metadata + traceability)
       CUSTOMER         — deduplicated customers referenced by orders
       PRODUCT          — deduplicated products referenced by orders
       ECOMMERCE_ORDER  — one row per order line (the fact table)
       ANALYSIS_RUN      — one row per POST /analyze call (history, Phase 1+2)
       ANOMALY           — Z-score flagged rows (populated starting Phase 2A)
       INSIGHT           — narrative insight lines (populated starting Phase 2A)

   Normalization:
       1NF — every column holds a single atomic value, no repeating groups.
       2NF — non-key attributes depend on the WHOLE primary key. Since every
             table here uses a single-column surrogate/natural key, there are
             no partial-key dependencies to begin with.
       3NF — non-key attributes depend ONLY on the key, not on each other.
             customer_email depends on customer_id, not on order_id, so it was
             pulled out of ECOMMERCE_ORDER into CUSTOMER. product_name and
             category depend on product_id, not on order_id, so they were
             pulled out into PRODUCT. This removes the transitive dependency
             order_id -> product_id -> product_name that a flat table would have.
   ============================================================================ */

IF DB_ID('IDAA_DB') IS NOT NULL
BEGIN
    ALTER DATABASE IDAA_DB SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE IDAA_DB;
END
GO

CREATE DATABASE IDAA_DB;
GO

USE IDAA_DB;
GO

/* ----------------------------------------------------------------------
   DATASET — one row per successful /upload call.
   Keeps the raw CSV traceable back to a database-backed run.
---------------------------------------------------------------------- */
CREATE TABLE dbo.Dataset (
    dataset_id      INT IDENTITY(1,1)      NOT NULL,
    file_id         UNIQUEIDENTIFIER       NOT NULL,   -- matches FastAPI file_id (uuid4)
    filename        NVARCHAR(260)          NOT NULL,
    schema_type     VARCHAR(20)            NOT NULL,
    uploaded_at     DATETIME2              NOT NULL CONSTRAINT DF_Dataset_UploadedAt DEFAULT SYSUTCDATETIME(),
    rows_ingested   INT                    NOT NULL,
    raw_file_path   NVARCHAR(500)          NULL,       -- filesystem path of the original CSV

    CONSTRAINT PK_Dataset PRIMARY KEY (dataset_id),
    CONSTRAINT UQ_Dataset_FileId UNIQUE (file_id),
    CONSTRAINT CK_Dataset_SchemaType CHECK (schema_type IN ('ecommerce','cafe','gym')),
    CONSTRAINT CK_Dataset_RowsIngested CHECK (rows_ingested >= 0)
);
GO

/* ----------------------------------------------------------------------
   CUSTOMER — deduplicated across orders. customer_id comes from the CSV
   (e.g. "CUST0001"); it is NOT globally unique across different datasets
   in real life, but for a single-tenant student project we key on it
   directly and keep the latest known email.
---------------------------------------------------------------------- */
CREATE TABLE dbo.Customer (
    customer_id     VARCHAR(50)            NOT NULL,
    customer_email  VARCHAR(254)           NULL,

    CONSTRAINT PK_Customer PRIMARY KEY (customer_id)
);
GO

/* ----------------------------------------------------------------------
   PRODUCT — deduplicated across orders.
---------------------------------------------------------------------- */
CREATE TABLE dbo.Product (
    product_id      VARCHAR(50)            NOT NULL,
    product_name    NVARCHAR(200)          NOT NULL,
    category        VARCHAR(30)            NOT NULL,

    CONSTRAINT PK_Product PRIMARY KEY (product_id),
    CONSTRAINT CK_Product_Category CHECK (category IN
        ('electronics','clothing','footwear','beauty','home','sports','food','books','toys','other'))
);
GO

/* ----------------------------------------------------------------------
   ECOMMERCE_ORDER — the fact table. One row per order line from the CSV.
   Mirrors ECOMMERCE_SCHEMA in app/schema/ecommerce_schema.py exactly.
---------------------------------------------------------------------- */
CREATE TABLE dbo.EcommerceOrder (
    order_row_id      INT IDENTITY(1,1)    NOT NULL,   -- surrogate PK (order_id can repeat across datasets/re-uploads)
    dataset_id        INT                  NOT NULL,
    order_id          VARCHAR(50)          NOT NULL,
    order_date        DATE                 NOT NULL,
    order_status      VARCHAR(20)          NOT NULL,
    customer_id       VARCHAR(50)          NOT NULL,
    product_id        VARCHAR(50)          NOT NULL,
    unit_price        DECIMAL(12,2)        NOT NULL,
    quantity          INT                  NOT NULL,
    discount          DECIMAL(5,2)         NULL,
    total_amount      DECIMAL(14,2)        NOT NULL,
    payment_method    VARCHAR(20)          NOT NULL,
    shipping_city     NVARCHAR(100)        NULL,
    shipping_state    NVARCHAR(100)        NULL,
    return_requested  VARCHAR(3)           NULL,

    CONSTRAINT PK_EcommerceOrder PRIMARY KEY (order_row_id),

    CONSTRAINT FK_Order_Dataset FOREIGN KEY (dataset_id)
        REFERENCES dbo.Dataset(dataset_id) ON DELETE CASCADE,

    CONSTRAINT FK_Order_Customer FOREIGN KEY (customer_id)
        REFERENCES dbo.Customer(customer_id),

    CONSTRAINT FK_Order_Product FOREIGN KEY (product_id)
        REFERENCES dbo.Product(product_id),

    CONSTRAINT CK_Order_Status CHECK (order_status IN
        ('pending','confirmed','shipped','delivered','cancelled','returned')),
    CONSTRAINT CK_Order_PaymentMethod CHECK (payment_method IN
        ('credit_card','debit_card','upi','net_banking','wallet','cod','emi')),
    CONSTRAINT CK_Order_ReturnRequested CHECK (return_requested IN ('yes','no') OR return_requested IS NULL),
    CONSTRAINT CK_Order_UnitPrice CHECK (unit_price >= 0),
    CONSTRAINT CK_Order_Quantity CHECK (quantity BETWEEN 1 AND 10000),
    CONSTRAINT CK_Order_Discount CHECK (discount IS NULL OR discount BETWEEN 0 AND 100),
    CONSTRAINT CK_Order_TotalAmount CHECK (total_amount >= 0),

    -- one physical CSV row shouldn't be ingested twice for the same dataset
    CONSTRAINT UQ_Order_DatasetOrderId UNIQUE (dataset_id, order_id)
);
GO

CREATE INDEX IX_Order_Dataset   ON dbo.EcommerceOrder(dataset_id);
CREATE INDEX IX_Order_Customer  ON dbo.EcommerceOrder(customer_id);
CREATE INDEX IX_Order_Product   ON dbo.EcommerceOrder(product_id);
CREATE INDEX IX_Order_OrderDate ON dbo.EcommerceOrder(order_date);
GO

/* ----------------------------------------------------------------------
   ANALYSIS_RUN — one row per POST /analyze/{file_id} call.
   Populated starting Phase 1 (metadata only). Gives faculty a visible
   "analysis history" table even before Phase 2 wires up full persistence.
---------------------------------------------------------------------- */
CREATE TABLE dbo.AnalysisRun (
    run_id          INT IDENTITY(1,1)      NOT NULL,
    dataset_id      INT                    NOT NULL,
    run_at          DATETIME2              NOT NULL CONSTRAINT DF_AnalysisRun_RunAt DEFAULT SYSUTCDATETIME(),
    schema_type     VARCHAR(20)            NOT NULL,
    total_revenue   DECIMAL(14,2)          NULL,
    total_orders    INT                    NULL,

    CONSTRAINT PK_AnalysisRun PRIMARY KEY (run_id),
    CONSTRAINT FK_Run_Dataset FOREIGN KEY (dataset_id)
        REFERENCES dbo.Dataset(dataset_id) ON DELETE CASCADE
);
GO

/* ----------------------------------------------------------------------
   ANOMALY — Z-score flagged rows, one row per flagged (column, row_index)
   pair from app/services/anomaly.py. Populated starting Phase 2A via
   crud.persist_anomalies(), called from POST /analyze/{file_id}.
---------------------------------------------------------------------- */
CREATE TABLE dbo.Anomaly (
    anomaly_id      INT IDENTITY(1,1)      NOT NULL,
    run_id          INT                    NOT NULL,
    column_name     VARCHAR(50)            NOT NULL,
    row_index       INT                    NOT NULL,
    value           DECIMAL(14,4)          NULL,
    z_score         DECIMAL(6,2)           NOT NULL,
    anomaly_type    VARCHAR(10)            NOT NULL,

    CONSTRAINT PK_Anomaly PRIMARY KEY (anomaly_id),
    CONSTRAINT FK_Anomaly_Run FOREIGN KEY (run_id)
        REFERENCES dbo.AnalysisRun(run_id) ON DELETE CASCADE,
    CONSTRAINT CK_Anomaly_Type CHECK (anomaly_type IN ('spike','drop'))
);
GO

/* ----------------------------------------------------------------------
   INSIGHT — narrative insight lines (Gemini or fallback), one row per
   narration line. Populated starting Phase 2A via crud.persist_insights(),
   called from POST /analyze/{file_id}. category is left NULL in Phase 2A
   (see app/db/crud.py docstring).
---------------------------------------------------------------------- */
CREATE TABLE dbo.Insight (
    insight_id      INT IDENTITY(1,1)      NOT NULL,
    run_id          INT                    NOT NULL,
    insight_text    NVARCHAR(1000)         NOT NULL,
    category        VARCHAR(30)            NULL,

    CONSTRAINT PK_Insight PRIMARY KEY (insight_id),
    CONSTRAINT FK_Insight_Run FOREIGN KEY (run_id)
        REFERENCES dbo.AnalysisRun(run_id) ON DELETE CASCADE
);
GO

PRINT 'IDAA_DB created successfully — 7 tables, PK/FK/CHECK constraints and indexes in place.';
GO
