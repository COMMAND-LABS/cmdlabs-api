"""Generate data/mock/duty_spend.csv — synthetic monthly customs-duty history.

Deterministic (fixed seed) so the committed file is reproducible. The shape is
what the forecast tool needs to have something to learn: a gentle upward trend,
yearly seasonality (Q4 peak), noise, and one tariff step-change two-thirds of
the way through so "what if the rate changes" is a real question.

Columns: month, import_value, duty_rate, duty_spend
"""
import csv
import math
import random
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "mock" / "duty_spend.csv"
START = date(2021, 1, 1)
MONTHS = 60
BASE_IMPORT = 2_400_000.0


def main() -> None:
    rng = random.Random(7)
    rows = []
    for i in range(MONTHS):
        year = START.year + (START.month - 1 + i) // 12
        month = (START.month - 1 + i) % 12 + 1
        trend = 1.0 + 0.006 * i
        season = 1.0 + 0.12 * math.sin((month - 4) / 12 * 2 * math.pi)
        noise = rng.gauss(1.0, 0.04)
        import_value = round(BASE_IMPORT * trend * season * noise, 2)
        duty_rate = 0.065 if i < 40 else 0.085  # tariff increase in month 41
        rows.append({
            "month": f"{year:04d}-{month:02d}-01",
            "import_value": import_value,
            "duty_rate": duty_rate,
            "duty_spend": round(import_value * duty_rate, 2),
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
