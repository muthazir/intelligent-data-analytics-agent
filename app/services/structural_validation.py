from fastapi import HTTPException
import pandas as pd


def validate_structure(df: pd.DataFrame):

    # 1. Check dataset empty
    if df.empty:
        raise HTTPException(
            status_code=400,
            detail="Dataset is empty"
        )

    # 2. Check columns exist
    if len(df.columns) == 0:
        raise HTTPException(
            status_code=400,
            detail="Dataset has no columns"
        )

    # 3. Check duplicate columns
    if df.columns.duplicated().any():
        raise HTTPException(
            status_code=400,
            detail="Dataset contains duplicate column names"
        )

    # 4. Minimum rows requirement
    if len(df) < 5:
        raise HTTPException(
            status_code=400,
            detail="Dataset must contain at least 5 rows"
        )

    # 5. All values empty
    if df.dropna(how="all").empty:
        raise HTTPException(
            status_code=400,
            detail="Dataset contains only empty values"
        )

    return True