"""
models.py
---------
SQLAlchemy ORM models — must stay in structural sync with the database as
created by sql/schema.sql PLUS every script in sql/migrations/ (in order).

If you change one, change the other. init_db() in session.py can create
these directly from the ORM (useful for automated tests), but the primary,
faculty-facing path is running sql/schema.sql + migrations by hand in SSMS.

Migration 001 (dataset-scoped entities):
    Customers and products are weak entities of Dataset — DatasetCustomer
    and DatasetProduct are keyed by (dataset_id, customer_id) and
    (dataset_id, product_id), and EcommerceOrder references them through
    composite foreign keys, so an order can only point at a customer/product
    of the SAME dataset. The original global Customer / Product tables are
    kept (with their rows) as legacy tables and are no longer written.
"""

from __future__ import annotations

import uuid
import datetime

from sqlalchemy import (
    Column, Integer, String, Numeric, Date, DateTime, UnicodeText,
    ForeignKey, ForeignKeyConstraint, CheckConstraint, UniqueConstraint, Uuid,
)
from sqlalchemy.orm import relationship

from app.db.session import Base


class Dataset(Base):
    __tablename__ = "Dataset"

    dataset_id    = Column(Integer, primary_key=True, autoincrement=True)
    file_id       = Column(Uuid, nullable=False, unique=True, default=uuid.uuid4)
    filename      = Column(String(260), nullable=False)
    schema_type   = Column(String(20), nullable=False)
    uploaded_at   = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    rows_ingested = Column(Integer, nullable=False)
    raw_file_path = Column(String(500), nullable=True)

    orders    = relationship("EcommerceOrder", back_populates="dataset", cascade="all, delete-orphan")
    customers = relationship("DatasetCustomer", back_populates="dataset", cascade="all, delete-orphan")
    products  = relationship("DatasetProduct", back_populates="dataset", cascade="all, delete-orphan")
    runs      = relationship("AnalysisRun", back_populates="dataset", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("schema_type IN ('ecommerce','cafe','gym')", name="CK_Dataset_SchemaType"),
        CheckConstraint("rows_ingested >= 0", name="CK_Dataset_RowsIngested"),
    )


class Customer(Base):
    """LEGACY (pre-migration 001) global customer table. Kept with its rows for
    history; the application no longer reads or writes it — see DatasetCustomer."""
    __tablename__ = "Customer"

    customer_id    = Column(String(50), primary_key=True)
    customer_email = Column(String(254), nullable=True)


class Product(Base):
    """LEGACY (pre-migration 001) global product table. Kept with its rows for
    history; the application no longer reads or writes it — see DatasetProduct."""
    __tablename__ = "Product"

    product_id   = Column(String(50), primary_key=True)
    product_name = Column(String(200), nullable=False)
    category     = Column(String(30), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "category IN ('electronics','clothing','footwear','beauty','home',"
            "'sports','food','books','toys','other')",
            name="CK_Product_Category",
        ),
    )


class DatasetCustomer(Base):
    """Customer as seen in ONE dataset (migration 001)."""
    __tablename__ = "DatasetCustomer"

    dataset_id     = Column(Integer, ForeignKey("Dataset.dataset_id", ondelete="CASCADE"), primary_key=True)
    customer_id    = Column(String(50), primary_key=True)
    customer_email = Column(String(254), nullable=True)

    dataset = relationship("Dataset", back_populates="customers")


class DatasetProduct(Base):
    """Product as seen in ONE dataset (migration 001)."""
    __tablename__ = "DatasetProduct"

    dataset_id   = Column(Integer, ForeignKey("Dataset.dataset_id", ondelete="CASCADE"), primary_key=True)
    product_id   = Column(String(50), primary_key=True)
    product_name = Column(String(200), nullable=False)
    category     = Column(String(30), nullable=False)

    dataset = relationship("Dataset", back_populates="products")

    __table_args__ = (
        CheckConstraint(
            "category IN ('electronics','clothing','footwear','beauty','home',"
            "'sports','food','books','toys','other')",
            name="CK_DatasetProduct_Category",
        ),
    )


class EcommerceOrder(Base):
    __tablename__ = "EcommerceOrder"

    order_row_id     = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id       = Column(Integer, ForeignKey("Dataset.dataset_id", ondelete="CASCADE"), nullable=False)
    order_id         = Column(String(50), nullable=False)
    order_date       = Column(Date, nullable=False)
    order_status     = Column(String(20), nullable=False)
    customer_id      = Column(String(50), nullable=False)
    product_id       = Column(String(50), nullable=False)
    unit_price       = Column(Numeric(12, 2), nullable=False)
    quantity         = Column(Integer, nullable=False)
    discount         = Column(Numeric(5, 2), nullable=True)
    total_amount     = Column(Numeric(14, 2), nullable=False)
    payment_method   = Column(String(20), nullable=False)
    shipping_city    = Column(String(100), nullable=True)
    shipping_state   = Column(String(100), nullable=True)
    return_requested = Column(String(3), nullable=True)

    dataset = relationship("Dataset", back_populates="orders")

    __table_args__ = (
        # Composite FKs: the referenced customer/product must belong to the
        # same dataset_id as the order (migration 001).
        ForeignKeyConstraint(
            ["dataset_id", "customer_id"],
            ["DatasetCustomer.dataset_id", "DatasetCustomer.customer_id"],
            name="FK_Order_DatasetCustomer",
        ),
        ForeignKeyConstraint(
            ["dataset_id", "product_id"],
            ["DatasetProduct.dataset_id", "DatasetProduct.product_id"],
            name="FK_Order_DatasetProduct",
        ),
        UniqueConstraint("dataset_id", "order_id", name="UQ_Order_DatasetOrderId"),
        CheckConstraint(
            "order_status IN ('pending','confirmed','shipped','delivered','cancelled','returned')",
            name="CK_Order_Status",
        ),
        CheckConstraint(
            "payment_method IN ('credit_card','debit_card','upi','net_banking','wallet','cod','emi')",
            name="CK_Order_PaymentMethod",
        ),
        CheckConstraint("unit_price >= 0", name="CK_Order_UnitPrice"),
        CheckConstraint("quantity BETWEEN 1 AND 10000", name="CK_Order_Quantity"),
        CheckConstraint("total_amount >= 0", name="CK_Order_TotalAmount"),
    )


class AnalysisRun(Base):
    __tablename__ = "AnalysisRun"

    run_id           = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id       = Column(Integer, ForeignKey("Dataset.dataset_id", ondelete="CASCADE"), nullable=False)
    run_at           = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    schema_type      = Column(String(20), nullable=False)
    total_revenue    = Column(Numeric(14, 2), nullable=True)
    total_orders     = Column(Integer, nullable=True)
    # Migration 001: canonical snapshot of the full /analyze response (NULL for
    # runs recorded before 001). SQL Server additionally enforces
    # ISJSON(result_json) = 1 (CK_AnalysisRun_ResultJson); that CHECK lives only
    # in the migration because SQLite (used by the test suite) has no ISJSON.
    result_json      = Column(UnicodeText, nullable=True)
    narration_source = Column(String(10), nullable=True)

    dataset   = relationship("Dataset", back_populates="runs")
    anomalies = relationship("Anomaly", back_populates="run", cascade="all, delete-orphan")
    insights  = relationship("Insight", back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "narration_source IS NULL OR narration_source IN ('gemini','fallback')",
            name="CK_AnalysisRun_NarrationSource",
        ),
    )


class Anomaly(Base):
    """One Z-score flagged (column, row_index) pair of one AnalysisRun."""
    __tablename__ = "Anomaly"

    anomaly_id  = Column(Integer, primary_key=True, autoincrement=True)
    run_id      = Column(Integer, ForeignKey("AnalysisRun.run_id", ondelete="CASCADE"), nullable=False)
    column_name = Column(String(50), nullable=False)
    row_index   = Column(Integer, nullable=False)
    value       = Column(Numeric(14, 4), nullable=True)
    z_score     = Column(Numeric(6, 2), nullable=False)
    anomaly_type = Column(String(10), nullable=False)

    run = relationship("AnalysisRun", back_populates="anomalies")

    __table_args__ = (
        CheckConstraint("anomaly_type IN ('spike','drop')", name="CK_Anomaly_Type"),
    )


class Insight(Base):
    """One narration line (Gemini or fallback) of one AnalysisRun."""
    __tablename__ = "Insight"

    insight_id   = Column(Integer, primary_key=True, autoincrement=True)
    run_id       = Column(Integer, ForeignKey("AnalysisRun.run_id", ondelete="CASCADE"), nullable=False)
    insight_text = Column(String(1000), nullable=False)
    category     = Column(String(30), nullable=True)

    run = relationship("AnalysisRun", back_populates="insights")
