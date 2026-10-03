/* ============================================================================
   IDAA — Migration 001
   Dataset-scoped Customer/Product + persisted analysis results
   ----------------------------------------------------------------------------
   ADDITIVE migration for an EXISTING IDAA database. It never drops or
   recreates the database and never deletes existing rows.

   Fresh install order:   sql/schema.sql   →   sql/migrations/001_*.sql
   (schema.sql is the destructive bootstrap script — only for a brand-new DB.)

   Run against a specific database (no USE statement on purpose):
       sqlcmd -S localhost -E -b -d IDAA_DB -i sql\migrations\001_dataset_scoped_entities_and_result_json.sql

   What it does
   ------------
   1. DatasetCustomer / DatasetProduct — customers and products are now
      WEAK ENTITIES of Dataset, keyed by (dataset_id, customer_id) and
      (dataset_id, product_id). The same CSV id (e.g. "P001") can exist in
      two datasets with different names/categories without contaminating
      each other.
   2. Backfills both tables from the existing orders + legacy Customer /
      Product tables (read-only use of the legacy tables).
   3. Composite foreign keys from EcommerceOrder:
          (dataset_id, customer_id) → DatasetCustomer
          (dataset_id, product_id)  → DatasetProduct
      so an order can only reference a customer/product of the SAME dataset.
   4. Drops the two legacy single-column FKs FK_Order_Customer and
      FK_Order_Product (constraint-only change; no data is removed). The
      legacy Customer / Product tables and all their rows are KEPT, frozen,
      and no longer written by the application.
   5. AnalysisRun.result_json (NVARCHAR(MAX), must be valid JSON) and
      AnalysisRun.narration_source ('gemini' | 'fallback') — the database
      becomes the authoritative store of completed analyses.
   6. SchemaVersion table recording that 001 was applied.

   Safety
   ------
   - Single transaction with XACT_ABORT: any failure rolls back everything.
   - Idempotent: every step checks whether it was already applied, so
     re-running the script is a no-op.
   - Verifies backfill completeness before COMMIT; throws (→ rollback) on
     any mismatch.
   - Undo: sql/migrations/001_rollback.sql (or RESTORE from the pre-migration
     COPY_ONLY backup).
   ============================================================================ */

SET NOCOUNT ON;
SET XACT_ABORT ON;

IF OBJECT_ID(N'dbo.EcommerceOrder', N'U') IS NULL
   OR OBJECT_ID(N'dbo.Dataset', N'U') IS NULL
   OR OBJECT_ID(N'dbo.AnalysisRun', N'U') IS NULL
BEGIN
    RAISERROR('Migration 001 aborted: this database does not contain the IDAA tables (Dataset/EcommerceOrder/AnalysisRun). Check the -d argument.', 16, 1);
    RETURN;
END;

BEGIN TRY
    BEGIN TRANSACTION;

    /* -- 1. DatasetCustomer --------------------------------------------- */
    IF OBJECT_ID(N'dbo.DatasetCustomer', N'U') IS NULL
        EXEC(N'
        CREATE TABLE dbo.DatasetCustomer (
            dataset_id      INT            NOT NULL,
            customer_id     VARCHAR(50)    NOT NULL,
            customer_email  VARCHAR(254)   NULL,
            CONSTRAINT PK_DatasetCustomer PRIMARY KEY (dataset_id, customer_id),
            CONSTRAINT FK_DatasetCustomer_Dataset FOREIGN KEY (dataset_id)
                REFERENCES dbo.Dataset(dataset_id) ON DELETE CASCADE
        );');

    /* -- 2. DatasetProduct ---------------------------------------------- */
    IF OBJECT_ID(N'dbo.DatasetProduct', N'U') IS NULL
        EXEC(N'
        CREATE TABLE dbo.DatasetProduct (
            dataset_id      INT            NOT NULL,
            product_id      VARCHAR(50)    NOT NULL,
            product_name    NVARCHAR(200)  NOT NULL,
            category        VARCHAR(30)    NOT NULL,
            CONSTRAINT PK_DatasetProduct PRIMARY KEY (dataset_id, product_id),
            CONSTRAINT FK_DatasetProduct_Dataset FOREIGN KEY (dataset_id)
                REFERENCES dbo.Dataset(dataset_id) ON DELETE CASCADE,
            CONSTRAINT CK_DatasetProduct_Category CHECK (category IN
                (''electronics'',''clothing'',''footwear'',''beauty'',''home'',
                 ''sports'',''food'',''books'',''toys'',''other''))
        );');

    /* -- 3. Backfill from existing orders (legacy tables are only READ) -- */
    EXEC(N'
    INSERT INTO dbo.DatasetCustomer (dataset_id, customer_id, customer_email)
    SELECT DISTINCT o.dataset_id, o.customer_id, c.customer_email
    FROM dbo.EcommerceOrder o
    JOIN dbo.Customer c ON c.customer_id = o.customer_id
    WHERE NOT EXISTS (SELECT 1 FROM dbo.DatasetCustomer dc
                      WHERE dc.dataset_id = o.dataset_id AND dc.customer_id = o.customer_id);');

    EXEC(N'
    INSERT INTO dbo.DatasetProduct (dataset_id, product_id, product_name, category)
    SELECT DISTINCT o.dataset_id, o.product_id, p.product_name, p.category
    FROM dbo.EcommerceOrder o
    JOIN dbo.Product p ON p.product_id = o.product_id
    WHERE NOT EXISTS (SELECT 1 FROM dbo.DatasetProduct dp
                      WHERE dp.dataset_id = o.dataset_id AND dp.product_id = o.product_id);');

    /* -- 4. Composite FKs (WITH CHECK validates every existing order) ---- */
    IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Order_DatasetCustomer')
        EXEC(N'
        ALTER TABLE dbo.EcommerceOrder WITH CHECK
            ADD CONSTRAINT FK_Order_DatasetCustomer FOREIGN KEY (dataset_id, customer_id)
            REFERENCES dbo.DatasetCustomer (dataset_id, customer_id);');

    IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Order_DatasetProduct')
        EXEC(N'
        ALTER TABLE dbo.EcommerceOrder WITH CHECK
            ADD CONSTRAINT FK_Order_DatasetProduct FOREIGN KEY (dataset_id, product_id)
            REFERENCES dbo.DatasetProduct (dataset_id, product_id);');

    /* -- 5. Retire legacy single-column FKs (constraints only, no data) -- */
    IF EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Order_Customer')
        EXEC(N'ALTER TABLE dbo.EcommerceOrder DROP CONSTRAINT FK_Order_Customer;');

    IF EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Order_Product')
        EXEC(N'ALTER TABLE dbo.EcommerceOrder DROP CONSTRAINT FK_Order_Product;');

    /* -- 6. AnalysisRun: persisted result + narration source ------------- */
    IF COL_LENGTH(N'dbo.AnalysisRun', N'result_json') IS NULL
        EXEC(N'ALTER TABLE dbo.AnalysisRun ADD result_json NVARCHAR(MAX) NULL;');

    IF COL_LENGTH(N'dbo.AnalysisRun', N'narration_source') IS NULL
        EXEC(N'ALTER TABLE dbo.AnalysisRun ADD narration_source VARCHAR(10) NULL;');

    IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_AnalysisRun_ResultJson')
        EXEC(N'
        ALTER TABLE dbo.AnalysisRun WITH CHECK
            ADD CONSTRAINT CK_AnalysisRun_ResultJson
            CHECK (result_json IS NULL OR ISJSON(result_json) = 1);');

    IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_AnalysisRun_NarrationSource')
        EXEC(N'
        ALTER TABLE dbo.AnalysisRun WITH CHECK
            ADD CONSTRAINT CK_AnalysisRun_NarrationSource
            CHECK (narration_source IS NULL OR narration_source IN (''gemini'',''fallback''));');

    /* -- 7. SchemaVersion ---------------------------------------------- */
    IF OBJECT_ID(N'dbo.SchemaVersion', N'U') IS NULL
        EXEC(N'
        CREATE TABLE dbo.SchemaVersion (
            version     VARCHAR(20)  NOT NULL,
            applied_at  DATETIME2    NOT NULL CONSTRAINT DF_SchemaVersion_AppliedAt DEFAULT SYSUTCDATETIME(),
            description NVARCHAR(200) NULL,
            CONSTRAINT PK_SchemaVersion PRIMARY KEY (version)
        );');

    EXEC(N'
    IF NOT EXISTS (SELECT 1 FROM dbo.SchemaVersion WHERE version = ''001'')
        INSERT INTO dbo.SchemaVersion (version, description)
        VALUES (''001'', N''Dataset-scoped Customer/Product + AnalysisRun.result_json/narration_source'');');

    /* -- 8. Verify before commit ----------------------------------------- */
    EXEC(N'
    DECLARE @expected_c INT = (SELECT COUNT(*) FROM (SELECT DISTINCT dataset_id, customer_id FROM dbo.EcommerceOrder) x);
    DECLARE @expected_p INT = (SELECT COUNT(*) FROM (SELECT DISTINCT dataset_id, product_id  FROM dbo.EcommerceOrder) x);
    DECLARE @orphans_c  INT = (SELECT COUNT(*) FROM dbo.EcommerceOrder o
                               WHERE NOT EXISTS (SELECT 1 FROM dbo.DatasetCustomer dc
                                                 WHERE dc.dataset_id = o.dataset_id AND dc.customer_id = o.customer_id));
    DECLARE @orphans_p  INT = (SELECT COUNT(*) FROM dbo.EcommerceOrder o
                               WHERE NOT EXISTS (SELECT 1 FROM dbo.DatasetProduct dp
                                                 WHERE dp.dataset_id = o.dataset_id AND dp.product_id = o.product_id));
    IF @orphans_c > 0 OR @orphans_p > 0
        THROW 50001, ''Migration 001 verification failed: some orders have no dataset-scoped customer/product row.'', 1;
    IF (SELECT COUNT(*) FROM dbo.DatasetCustomer dc
        WHERE EXISTS (SELECT 1 FROM dbo.EcommerceOrder o WHERE o.dataset_id = dc.dataset_id AND o.customer_id = dc.customer_id)) <> @expected_c
       OR (SELECT COUNT(*) FROM dbo.DatasetProduct dp
        WHERE EXISTS (SELECT 1 FROM dbo.EcommerceOrder o WHERE o.dataset_id = dp.dataset_id AND o.product_id = dp.product_id)) <> @expected_p
        THROW 50002, ''Migration 001 verification failed: dataset-scoped row counts do not match the distinct pairs in EcommerceOrder.'', 1;
    IF EXISTS (SELECT 1 FROM sys.foreign_keys
               WHERE name IN (N''FK_Order_DatasetCustomer'', N''FK_Order_DatasetProduct'')
                 AND (is_not_trusted = 1 OR is_disabled = 1))
        THROW 50003, ''Migration 001 verification failed: composite foreign keys are not trusted.'', 1;');

    COMMIT TRANSACTION;
    PRINT 'Migration 001 applied (or already present) and verified.';
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;
