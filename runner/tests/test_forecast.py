"""The forecast is generic, returns a range, and refuses what it cannot model."""

import math

import pandas as pd
import pytest

from runner.forecast import MIN_ROWS, ForecastError, forecast


def _series(n: int = 60, rate: float = 0.065) -> pd.DataFrame:
    months = pd.date_range("2021-01-01", periods=n, freq="MS")
    volume = [1000 * (1 + 0.005 * i) * (1 + 0.1 * math.sin(i / 12 * 2 * math.pi)) for i in range(n)]
    return pd.DataFrame({"period": months, "volume": volume, "price": rate})


@pytest.mark.parametrize("model_name", ["lightgbm", "random_forest"])
def test_returns_a_range_around_the_point(model_name):
    out = forecast(_series(), date_column="period", target_column="volume", model_name=model_name)
    assert out["period"] == "2026-01"
    assert out["low"] <= out["prediction"] <= out["high"]
    assert out["model"] == model_name
    assert 0 <= out["holdout_mape"] < 1
    assert "rate" not in out


def test_rate_column_derives_a_second_series_and_honours_an_override():
    base = forecast(_series(), date_column="period", target_column="volume", rate_column="price")
    assert base["rate"]["value"] == pytest.approx(0.065)
    assert base["rate"]["overridden"] is False
    assert base["rate"]["derived_prediction"] == pytest.approx(base["prediction"] * 0.065, rel=1e-3)

    what_if = forecast(_series(), date_column="period", target_column="volume",
                       rate_column="price", rate_override=0.10)
    assert what_if["rate"]["overridden"] is True
    assert what_if["rate"]["derived_prediction"] == pytest.approx(what_if["prediction"] * 0.10, rel=1e-3)
    assert what_if["rate"]["last_actual_rate"] == pytest.approx(0.065)


def test_columns_are_not_hard_coded():
    df = _series().rename(columns={"period": "month", "volume": "import_value"})
    out = forecast(df, date_column="month", target_column="import_value")
    assert out["target"] == "import_value"


def test_unsorted_input_is_ordered_by_date():
    shuffled = _series().sample(frac=1, random_state=1)
    out = forecast(shuffled, date_column="period", target_column="volume")
    assert out["period"] == "2026-01"


@pytest.mark.parametrize("kwargs, match", [
    ({"date_column": "nope", "target_column": "volume"}, "not found"),
    ({"date_column": "period", "target_column": "volume", "model_name": "prophet"}, "Unknown model"),
    ({"date_column": "period", "target_column": "volume", "rate_override": 0.1}, "no rate column"),
])
def test_bad_requests_raise_forecast_error(kwargs, match):
    with pytest.raises(ForecastError, match=match):
        forecast(_series(), **kwargs)


def test_too_little_history_is_refused():
    with pytest.raises(ForecastError, match="at least"):
        forecast(_series(n=MIN_ROWS - 1), date_column="period", target_column="volume")
