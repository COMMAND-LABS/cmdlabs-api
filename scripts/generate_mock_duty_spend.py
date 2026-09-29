"""Generate data/mock/duty_spend*.csv — synthetic monthly customs-duty history.

Deterministic (fixed seeds) so the committed files are reproducible. The shape
is what the forecast tool needs to have something to learn: a trend, yearly
seasonality, noise, and tariff step-changes so "what if the rate changes" is a
real question.

Datasets:
- duty_spend.csv          60 months from 2021-01. Gentle upward trend, Q4
                          peak, one tariff increase two-thirds of the way in.
                          (Used by the tests — must stay byte-identical.)
- duty_spend_7yr.csv      84 months, 2019-09 through 2026-08. Larger importer,
                          two tariff increases and a 2020 volume dip.
- duty_spend_volatile.csv 36 months, 2023-09 through 2026-08. Smaller importer,
                          spring peak, noisier, a tariff cut then a hike.

Columns: month, import_value, duty_rate, duty_spend
"""
import csv
import math
import random
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "mock"


@dataclass
class Dataset:
    filename: str
    start: date
    months: int
    base_import: float
    seed: int
    trend_per_month: float
    season_amplitude: float
    peak_month: int  # month (1-12) where seasonality is highest
    noise_sd: float
    # (first month index it applies from, rate), in ascending order.
    rate_schedule: list[tuple[int, float]]
    # month index -> volume multiplier, for one-off shocks.
    shocks: dict[int, float] = field(default_factory=dict)


DATASETS = [
    # peak_month=7 reproduces the original sin((month - 4) / 12 * 2π) curve.
    # (It peaks in July, not Q4 as this file once said; kept byte-identical.)
    Dataset("duty_spend.csv", date(2021, 1, 1), 60, 2_400_000.0, seed=7,
            trend_per_month=0.006, season_amplitude=0.12, peak_month=7,
            noise_sd=0.04, rate_schedule=[(0, 0.065), (40, 0.085)]),
    Dataset("duty_spend_7yr.csv", date(2019, 9, 1), 84, 3_100_000.0, seed=19,
            trend_per_month=0.005, season_amplitude=0.15, peak_month=11,
            noise_sd=0.035,
            # 2019-09 5.5%; 2022-01 +2pt; 2025-04 +2.5pt.
            rate_schedule=[(0, 0.055), (28, 0.075), (67, 0.10)],
            # 2020-04..2020-07: pandemic-style volume dip and recovery.
            shocks={7: 0.62, 8: 0.70, 9: 0.82, 10: 0.93}),
    Dataset("duty_spend_volatile.csv", date(2023, 9, 1), 36, 850_000.0, seed=42,
            trend_per_month=0.004, season_amplitude=0.22, peak_month=4,
            noise_sd=0.09,
            # 2023-09 8%; 2024-06 cut to 6.5%; 2025-10 hike to 11%.
            rate_schedule=[(0, 0.08), (9, 0.065), (25, 0.11)],
            # 2025-02: one-off stock-up ahead of the announced hike.
            shocks={17: 1.35}),
]


def rate_for(schedule: list[tuple[int, float]], i: int) -> float:
    return [rate for start, rate in schedule if start <= i][-1]


def build_rows(ds: Dataset) -> list[dict]:
    rng = random.Random(ds.seed)
    rows = []
    for i in range(ds.months):
        year = ds.start.year + (ds.start.month - 1 + i) // 12
        month = (ds.start.month - 1 + i) % 12 + 1
        trend = 1.0 + ds.trend_per_month * i
        season = 1.0 + ds.season_amplitude * math.sin((month - (ds.peak_month - 3)) / 12 * 2 * math.pi)
        noise = rng.gauss(1.0, ds.noise_sd)
        import_value = round(ds.base_import * trend * season * noise * ds.shocks.get(i, 1.0), 2)
        duty_rate = rate_for(ds.rate_schedule, i)
        rows.append({
            "month": f"{year:04d}-{month:02d}-01",
            "import_value": import_value,
            "duty_rate": duty_rate,
            "duty_spend": round(import_value * duty_rate, 2),
        })
    return rows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ds in DATASETS:
        rows = build_rows(ds)
        out = OUT_DIR / ds.filename
        with out.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
