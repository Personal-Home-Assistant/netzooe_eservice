# Copyright 2026 Michael Hacker
# SPDX-License-Identifier: Apache-2.0
"""Synthetic profile data; no customer identifiers or meter readings."""

from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from typing import Any
from typing import Final
from zoneinfo import ZoneInfo

MPAN: Final[str] = "AT0000000000000000000000011111112"
ACCOUNT: Final[str] = "100000000001"
STATISTIC_ID: Final[str] = f"netzooe_eservice:{MPAN.lower()}_consumption_profile"
DAY: Final[date] = date(2026, 6, 26)
HOURS: Final[int] = 24
INITIAL_COST: Final[float] = 6
CORRECTED_COST: Final[float] = 6.0625


def profile_data(day: date, *, last_day: date | None = None) -> list[dict[str, Any]]:
    """Build a complete local day using explicitly synthetic quarter-hour values."""
    start: datetime = datetime.combine(day, time(), ZoneInfo("Europe/Vienna")).astimezone(UTC)
    end: datetime = datetime.combine(
        (last_day or day) + timedelta(days=1), time(), ZoneInfo("Europe/Vienna")
    ).astimezone(UTC)
    values: list[dict[str, Any]] = []
    while start < end:
        values.append({"datetime": start.isoformat(), "value": 0.25, "status": "VALID"})
        start += timedelta(minutes=15)
    return [
        {
            "unit": "KWH",
            "granularity": "QUARTER_OF_AN_HOUR",
            "profileValues": values,
            "sum": {"value": len(values) * 0.25, "unit": "KWH"},
        }
    ]


def profile_catalog(day: date, *, last_day: date | None = None) -> list[dict[str, Any]]:
    """Return metadata for the synthetic point."""
    return [
        {
            "contractAccountNumber": ACCOUNT,
            "meterPointAdministrationNumber": MPAN,
            "profiles": [
                {
                    "from": day.isoformat(),
                    "to": (last_day or day).isoformat(),
                    "profileType": "ACTIVE_CURRENT",
                    "granularity": "QUARTER_OF_AN_HOUR",
                }
            ],
        }
    ]
