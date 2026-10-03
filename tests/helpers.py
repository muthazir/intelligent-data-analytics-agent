"""Shared builders / API helpers for the IDAA test suite."""

from __future__ import annotations

import os
import pathlib

import numpy as np
import pandas as pd

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLE_CSV = PROJECT_ROOT / "sample_data" / "ecommerce_sample.csv"
UPLOAD_DIR = "uploads"


def ecommerce_df() -> pd.DataFrame:
    return pd.read_csv(SAMPLE_CSV)


def cafe_df(n: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    df = pd.DataFrame({
        "order_id": [f"C{i:03d}" for i in range(n)],
        "order_date": pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d"),
        "customer_id": [f"CU{i % 9}" for i in range(n)],
        "item_name": rng.choice(["Latte", "Mocha", "Bagel"], n),
        "category": rng.choice(["coffee", "snacks"], n),
        "price": rng.uniform(80, 200, n).round(2),
        "quantity": rng.integers(1, 4, n),
        "payment_method": rng.choice(["cash", "card", "upi"], n),
        "store_location": "pune",
    })
    df.loc[5, "price"] = 5000.0          # guaranteed price spike
    return df


def gym_df(n: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    df = pd.DataFrame({
        "member_id": [f"M{i:03d}" for i in range(n)],
        "member_name": [f"Member {i}" for i in range(n)],
        "gender": rng.choice(["male", "female"], n),
        "age": rng.integers(18, 60, n),
        "membership_type": rng.choice(["monthly", "yearly"], n),
        "membership_start_date": "2024-01-01",
        "membership_end_date": "2024-12-31",
        "monthly_fee": rng.uniform(1000, 2000, n).round(0),
        "payment_status": rng.choice(["paid", "pending"], n),
        "attendance_days": rng.integers(10, 20, n),
    })
    df.loc[3, "monthly_fee"] = 20000.0   # guaranteed fee spike
    return df


def upload(client, df_or_bytes, name: str = "test.csv", schema: str | None = "ecommerce"):
    data = df_or_bytes if isinstance(df_or_bytes, bytes) else df_or_bytes.to_csv(index=False).encode("utf-8")
    url = "/upload" if schema is None else f"/upload?schema_type={schema}"
    return client.post(url, files={"file": (name, data, "text/csv")})


def upload_ok(client, df, name: str = "test.csv", schema: str = "ecommerce") -> dict:
    r = upload(client, df, name, schema)
    assert r.status_code == 200, r.text
    return r.json()


def analyze_ok(client, file_id: str) -> dict:
    r = client.post(f"/analyze/{file_id}")
    assert r.status_code == 200, r.text
    return r.json()


def local_file(file_id: str, suffix: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{file_id}_{suffix}")


def remove_local(file_id: str, *suffixes: str) -> None:
    for s in suffixes:
        p = local_file(file_id, s)
        if os.path.exists(p):
            os.remove(p)
