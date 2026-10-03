/* ============================================================================
   IDAA — Rollback for Migration 001
   ----------------------------------------------------------------------------
   Returns the database to the pre-001 structure WITHOUT deleting any
   Dataset / EcommerceOrder / AnalysisRun / Anomaly / Insight rows.

       sqlcmd -S localhost -E -b -d IDAA_DB -i sql\migrations\001_rollback.sql

   Steps
   -----
   1. Ids that only exist in DatasetCustomer / DatasetProduct (uploaded after
      001 was applied) are copied into the legacy Customer / Product tables,
      using the attributes from the lowest dataset_id, so the legacy FKs can
      be re-validated. Existing legacy rows are never changed.
   2. Legacy FKs FK_Order_Customer / FK_Order_Product re-added WITH CHECK.
   3. Composite FKs dropped.
   4. AnalysisRun.result_json / narration_source (and their CHECKs) dropped.
      AnalysisRun rows are kept — only the stored JSON snapshot is lost.
   5. DatasetCustomer, DatasetProduct and SchemaVersion dropped.

   LIMITATION: for datasets uploaded AFTER 001 was applied, the per-dataset
   customer emails / product names / categories exist only in the dropped
   DatasetCustomer / DatasetProduct tables. After this rollback those
   datasets fall back to the global first-seen values in Customer / Product
   (the pre-001 behaviour), and re-applying 001 cannot recover them.

   Full-fidelity alternative: RESTORE the COPY_ONLY backup taken before 001.
   NOTE: the pre-001 application code (git baseline) must be deployed again
   after running this, because the Stage 1 code expects the 001 objects.
   ============================================================================ */

SET NOCOUNT ON;
SET XACT_ABORT ON;

BEGIN TRY
    BEGIN TRANSACTION;

    /* -- 1. Copy post-001 ids back into the legacy tables ----------------- */
    IF OBJECT_ID(N'dbo.DatasetCustomer', N'U') IS NOT NULL
        EXEC(N'
        INSERT INTO dbo.Customer (customer_id, customer_email)
        SELECT dc.customer_id, dc.customer_email
        FROM dbo.DatasetCustomer dc
        WHERE dc.dataset_id = (SELECT MIN(d2.dataset_id) FROM dbo.DatasetCustomer d2
                               WHERE d2.customer_id = dc.customer_id)
          AND NOT EXISTS (SELECT 1 FROM dbo.Customer c WHERE c.customer_id = dc.customer_id);');

    IF OBJECT_ID(N'dbo.DatasetProduct', N'U') IS NOT NULL
        EXEC(N'
        INSERT INTO dbo.Product (product_id, product_name, category)
        SELECT dp.product_id, dp.product_name, dp.category
        FROM dbo.DatasetProduct dp
        WHERE dp.dataset_id = (SELECT MIN(d2.dataset_id) FROM dbo.DatasetProduct d2
                               WHERE d2.product_id = dp.product_id)
          AND NOT EXISTS (SELECT 1 FROM dbo.Product p WHERE p.product_id = dp.product_id);');

    /* -- 2. Re-add legacy FKs -------------------------------------------- */
    IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Order_Customer')
        EXEC(N'
        ALTER TABLE dbo.EcommerceOrder WITH CHECK
            ADD CONSTRAINT FK_Order_Customer FOREIGN KEY (customer_id)
            REFERENCES dbo.Customer (customer_id);');

    IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Order_Product')
        EXEC(N'
        ALTER TABLE dbo.EcommerceOrder WITH CHECK
            ADD CONSTRAINT FK_Order_Product FOREIGN KEY (product_id)
            REFERENCES dbo.Product (product_id);');

    /* -- 3. Drop composite FKs --------------------------------------------- */
    IF EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Order_DatasetCustomer')
        EXEC(N'ALTER TABLE dbo.EcommerceOrder DROP CONSTRAINT FK_Order_DatasetCustomer;');
    IF EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = N'FK_Order_DatasetProduct')
        EXEC(N'ALTER TABLE dbo.EcommerceOrder DROP CONSTRAINT FK_Order_DatasetProduct;');

    /* -- 4. AnalysisRun columns ------------------------------------------- */
    IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_AnalysisRun_ResultJson')
        EXEC(N'ALTER TABLE dbo.AnalysisRun DROP CONSTRAINT CK_AnalysisRun_ResultJson;');
    IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_AnalysisRun_NarrationSource')
        EXEC(N'ALTER TABLE dbo.AnalysisRun DROP CONSTRAINT CK_AnalysisRun_NarrationSource;');
    IF COL_LENGTH(N'dbo.AnalysisRun', N'result_json') IS NOT NULL
        EXEC(N'ALTER TABLE dbo.AnalysisRun DROP COLUMN result_json;');
    IF COL_LENGTH(N'dbo.AnalysisRun', N'narration_source') IS NOT NULL
        EXEC(N'ALTER TABLE dbo.AnalysisRun DROP COLUMN narration_source;');

    /* -- 5. Drop 001 tables ----------------------------------------------- */
    IF OBJECT_ID(N'dbo.DatasetCustomer', N'U') IS NOT NULL EXEC(N'DROP TABLE dbo.DatasetCustomer;');
    IF OBJECT_ID(N'dbo.DatasetProduct', N'U')  IS NOT NULL EXEC(N'DROP TABLE dbo.DatasetProduct;');
    IF OBJECT_ID(N'dbo.SchemaVersion', N'U')   IS NOT NULL EXEC(N'DROP TABLE dbo.SchemaVersion;');

    /* -- verify ------------------------------------------------------------ */
    EXEC(N'
    IF EXISTS (SELECT 1 FROM sys.foreign_keys
               WHERE name IN (N''FK_Order_Customer'', N''FK_Order_Product'')
                 AND (is_not_trusted = 1 OR is_disabled = 1))
       OR (SELECT COUNT(*) FROM sys.foreign_keys WHERE name IN (N''FK_Order_Customer'', N''FK_Order_Product'')) <> 2
        THROW 50011, ''Rollback 001 verification failed: legacy foreign keys missing or untrusted.'', 1;');

    COMMIT TRANSACTION;
    PRINT 'Rollback of migration 001 completed and verified.';
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;
