"""One-step-ahead forecast over a monthly series — the generic form of the
duty-spend prototype.

The model predicts the TARGET from its own history: calendar month plus lagged
values (1, 2, 3 and 12 months back). An optional RATE column derives a second
series as prediction × rate, which is how "duty spend = import value × duty
rate" generalises to "spend = volume × price". A tariff change is then just a
different rate, passed as `rate_override`.

What changed from the prototype, and why:
- Any date/target column, not hard-coded names.
- Returns a RANGE (point ± holdout MAPE), because "give the range, not the
  point" is in the agent's instructions and the prototype only returned MAPE.
- No MLflow. On Cloud Run its SQLite store would vanish with the instance;
  the metrics ride in the response instead, where the model and the user see
  them.
"""

from __future__ import annotations

import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_percentage_error

LAGS = (1, 2, 3, 12)
FEATURES = ["month_num"] + [f"lag_{lag}" for lag in LAGS]
MODELS = {
    "lightgbm": lambda: LGBMRegressor(n_estimators=200, learning_rate=0.05, verbose=-1),
    "random_forest": lambda: RandomForestRegressor(n_estimators=200, random_state=0),
}
# Enough rows that, after dropping the first max(LAGS) rows for lag features,
# the 70/15/15 split still leaves a handful of points in each part.
MIN_ROWS = max(LAGS) + 20


class ForecastError(ValueError):
    """A problem with the data or request that the caller can fix."""


def _prepare(df: pd.DataFrame, date_column: str, target_column: str,
             rate_column: str | None) -> pd.DataFrame:
    for col in (date_column, target_column, rate_column):
        if col and col not in df.columns:
            raise ForecastError(f"Column '{col}' not found. Columns: {list(df.columns)}")

    out = pd.DataFrame({
        "date": pd.to_datetime(df[date_column], errors="coerce"),
        "target": pd.to_numeric(df[target_column], errors="coerce"),
    })
    if rate_column:
        out["rate"] = pd.to_numeric(df[rate_column], errors="coerce")

    out = out.dropna(subset=["date", "target"]).sort_values("date").reset_index(drop=True)
    if len(out) < MIN_ROWS:
        raise ForecastError(
            f"Need at least {MIN_ROWS} rows with a valid date and target; got {len(out)}."
        )
    return out


def build_features(series: pd.DataFrame) -> pd.DataFrame:
    """Calendar month plus lagged target. Drops rows without a full lag history."""
    out = series[["date", "target"]].copy()
    out["month_num"] = out["date"].dt.month
    for lag in LAGS:
        out[f"lag_{lag}"] = out["target"].shift(lag)
    return out.dropna().reset_index(drop=True)


def split(features: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Chronological train / validation / holdout, 70 / 15 / 15."""
    n = len(features)
    a, b = int(n * 0.70), int(n * 0.85)
    return features.iloc[:a], features.iloc[a:b], features.iloc[b:]


def _mape(model, part: pd.DataFrame) -> float:
    return float(mean_absolute_percentage_error(part["target"], model.predict(part[FEATURES])))


def forecast(
    df: pd.DataFrame,
    *,
    date_column: str,
    target_column: str,
    model_name: str = "lightgbm",
    rate_column: str | None = None,
    rate_override: float | None = None,
) -> dict:
    """Train on the history and predict the next month. See module docstring."""
    if model_name not in MODELS:
        raise ForecastError(f"Unknown model '{model_name}'. Choose from {sorted(MODELS)}.")
    if rate_override is not None and not rate_column:
        raise ForecastError("rate_override given but this tool has no rate column configured.")

    series = _prepare(df, date_column, target_column, rate_column)
    features = build_features(series)
    train_df, val_df, holdout_df = split(features)

    model = MODELS[model_name]()
    model.fit(train_df[FEATURES], train_df["target"])
    val_mape = _mape(model, val_df)
    holdout_mape = _mape(model, holdout_df)

    # Refit on everything so the forecast uses the latest months.
    model.fit(features[FEATURES], features["target"])

    next_month = series["date"].iloc[-1] + pd.DateOffset(months=1)
    row = {"month_num": next_month.month}
    for lag in LAGS:
        row[f"lag_{lag}"] = float(series["target"].iloc[-lag])
    point = float(model.predict(pd.DataFrame([row])[FEATURES])[0])

    # The range is the point estimate widened by the out-of-sample error the
    # model actually made on the holdout months. Honest and simple; it is not
    # a calibrated confidence interval and the response says so.
    half_width = abs(point) * holdout_mape
    result = {
        "period": next_month.strftime("%Y-%m"),
        "target": target_column,
        "prediction": round(point, 2),
        "low": round(point - half_width, 2),
        "high": round(point + half_width, 2),
        "interval_basis": "prediction ± holdout MAPE (approximate, not a calibrated confidence interval)",
        "last_actual": round(float(series["target"].iloc[-1]), 2),
        "last_period": series["date"].iloc[-1].strftime("%Y-%m"),
        "model": model_name,
        "val_mape": round(val_mape, 4),
        "holdout_mape": round(holdout_mape, 4),
        "n_rows": int(len(series)),
    }

    if rate_column:
        last_rate = float(series["rate"].dropna().iloc[-1])
        rate = float(rate_override) if rate_override is not None else last_rate
        result["rate"] = {
            "column": rate_column,
            "value": rate,
            "overridden": rate_override is not None,
            "last_actual_rate": last_rate,
            "derived_prediction": round(point * rate, 2),
            "derived_low": round((point - half_width) * rate, 2),
            "derived_high": round((point + half_width) * rate, 2),
            "derived_last_actual": round(float(series["target"].iloc[-1]) * last_rate, 2),
        }
    return result
