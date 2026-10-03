"""
dtypes.py
---------
pandas-version-independent dtype checks.

pandas < 3 stores text columns as `object`; pandas >= 3 infers the dedicated
string dtype ("str") instead. Code that tested `series.dtype == object` to
find text columns silently skipped every text column under pandas 3, so the
text-processing steps (trimming, empty → NaN, lower-casing, currency
stripping, ...) use this helper instead.
"""

from __future__ import annotations

import pandas as pd


def is_text_series(series: pd.Series) -> bool:
    """True for text columns under both pandas 2 (object) and pandas 3 (str)."""
    dtype = series.dtype
    return pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype)
