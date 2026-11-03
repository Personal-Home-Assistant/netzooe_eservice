# Copyright 2026 Michael Hacker
# SPDX-License-Identifier: Apache-2.0
"""Import provider intervals at measurement time without changing entity statistics."""

from __future__ import annotations

import math
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfEnergy
from homeassistant.util import dt as dt_util
from netzooe_eservice_api.api import Pod
from netzooe_eservice_api.constants import ConsumptionsProfilesBranch
from netzooe_eservice_api.constants import Granularity
from netzooe_eservice_api.constants import ProfileType

from .const import DOMAIN
from .const import PROFILE_BACKFILL_DAYS
from .const import PROFILE_BATCH_DAYS
from .const import PROFILE_RECHECK_DAYS
from .const import PROFILE_TIME_ZONE
from .const import ProfileValueStatus

if TYPE_CHECKING:
    from homeassistant.components.recorder import Recorder
    from homeassistant.components.recorder.models import StatisticData
    from homeassistant.components.recorder.models import StatisticMetaData
    from homeassistant.components.recorder.statistics import StatisticsRow
    from homeassistant.core import HomeAssistant
    from netzooe_eservice_api.api import NetzOOEeServiceAPI

type IntervalValues = dict[datetime, float]


class ProfileStatisticsImporter:
    """Share the existing API session and rebuild a bounded correction window."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: NetzOOEeServiceAPI,
        *,
        energy_price: float | None = None,
        energy_price_from: date | None = None,
    ) -> None:
        """Initialize without copying or persisting credentials."""
        self.hass: HomeAssistant = hass
        self.api: NetzOOEeServiceAPI = api
        self.energy_price: float | None = energy_price
        self.energy_price_from: date | None = energy_price_from
        if energy_price is not None and (
            isinstance(energy_price, bool)
            or not math.isfinite(energy_price)
            or energy_price < 0
            or energy_price_from is None
        ):
            msg: str = "Invalid dated energy price"
            raise ValueError(msg)

    async def async_update(self) -> None:
        """Import available quarter-hour profiles, aggregating complete UTC hours."""
        points: list[dict[str, Any]] = await self.api.consumptions_profiles(
            ConsumptionsProfilesBranch.ELECTRICITY,
            active_only=True,
        )
        for point in points:
            for profile in point["profiles"]:
                if (
                    profile["profileType"] == ProfileType.ACTIVE_CURRENT.value
                    and profile["granularity"] == Granularity.QUARTER_OF_AN_HOUR.value
                ):
                    await self._async_import_profile(point, profile)

    async def _async_import_profile(self, point: dict[str, Any], profile: dict[str, Any], /) -> None:
        """Import one profile, preserving a cumulative anchor before any correction."""
        today: date = dt_util.now().astimezone(PROFILE_TIME_ZONE).date()
        first: date = max(date.fromisoformat(profile["from"]), today - timedelta(days=PROFILE_BACKFILL_DAYS))
        last: date = min(date.fromisoformat(profile["to"]), today - timedelta(days=1))
        if first > last:
            return

        statistic_id: str = f"{DOMAIN}:{point['meterPointAdministrationNumber'].lower()}_consumption_profile"
        recorder: Recorder = get_instance(self.hass)
        convert_units: bool = False
        latest: dict[str, list[StatisticsRow]] = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, convert_units, {"sum"}
        )
        start: date = first
        if latest.get(statistic_id):
            latest_start: datetime = datetime.fromtimestamp(latest[statistic_id][0]["start"], UTC)
            start = max(
                first, latest_start.astimezone(PROFILE_TIME_ZONE).date() - timedelta(days=PROFILE_RECHECK_DAYS - 1)
            )

        cost_id: str = statistic_id + "_energy_cost"
        if self.energy_price is not None:
            cost_latest: dict[str, list[StatisticsRow]] = await recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, cost_id, convert_units, {"sum"}
            )
            if not cost_latest.get(cost_id):
                start = first

        start_utc: datetime = self._midnight_utc(start)
        anchor: float | None = await self._async_previous_sum(statistic_id, start_utc)
        if anchor is None and start != first:
            start = first
        baseline: float = anchor or 0.0

        values: IntervalValues = {}
        cursor: date = start
        while cursor <= last:
            batch_last: date = min(last, cursor + timedelta(days=PROFILE_BATCH_DAYS - 1))
            response: list[dict[str, Any]] = await self.api.consumptions_profile(
                [
                    Pod(
                        contract_account_number=point["contractAccountNumber"],
                        meter_point_administration_number=point["meterPointAdministrationNumber"],
                        profile_type=ProfileType.ACTIVE_CURRENT,
                        best_available_granularity=Granularity.QUARTER_OF_AN_HOUR,
                        date_from=(cursor + timedelta(days=offset)).isoformat(),
                        date_to=(cursor + timedelta(days=offset)).isoformat(),
                    )
                    for offset in range((batch_last - cursor).days + 1)
                ]
            )
            values.update(self._validated_values(response, cursor, batch_last))
            cursor = batch_last + timedelta(days=1)

        stats: list[StatisticData] = self._hourly_statistics(values, baseline)
        metadata: StatisticMetaData = {
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "name": "Netz OÖ consumption profile",
            "unit_class": "energy",
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
        }
        costs: list[StatisticData] = await self._async_cost_statistics(cost_id, values, first, start)
        async_add_external_statistics(self.hass, metadata, stats)
        if costs:
            cost_metadata: StatisticMetaData = metadata | {
                "statistic_id": cost_id,
                "name": "Netz OÖ variable energy cost",
                "unit_class": None,
                "unit_of_measurement": self.hass.config.currency,
            }
            async_add_external_statistics(self.hass, cost_metadata, costs)
        await recorder.async_block_till_done()

    async def _async_cost_statistics(
        self, cost_id: str, values: IntervalValues, first: date, start: date, /
    ) -> list[StatisticData]:
        """Keep costs on measurement hours and preserve earlier tariff periods."""
        if self.energy_price is None or self.energy_price_from is None:
            return []
        cost_first: date = max(start, self.energy_price_from)
        cost_start: datetime = self._midnight_utc(cost_first)
        if cost_start > max(values):
            return []
        anchor: float | None = await self._async_previous_sum(cost_id, cost_start)
        if anchor is None and cost_first > max(first, self.energy_price_from):
            msg: str = "Missing cost statistics anchor"
            raise ValueError(msg)
        cost_values: IntervalValues = {
            stamp: value * self.energy_price for stamp, value in values.items() if stamp >= cost_start
        }
        return self._hourly_statistics(cost_values, anchor or 0.0)

    async def _async_previous_sum(self, statistic_id: str, stamp: datetime, /) -> float | None:
        """Load the cumulative anchor immediately before a replacement window."""
        base: dict[str, list[StatisticsRow]] = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            stamp - timedelta(hours=1),
            stamp,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        if not base.get(statistic_id):
            return None
        previous_sum: float | None = base[statistic_id][-1].get("sum")
        if previous_sum is None or not math.isfinite(previous_sum):
            msg: str = "Invalid statistics anchor"
            raise ValueError(msg)
        return previous_sum

    @staticmethod
    def _midnight_utc(day: date, /) -> datetime:
        """Convert a local calendar boundary, including daylight saving, into UTC."""
        return datetime.combine(day, time(), PROFILE_TIME_ZONE).astimezone(UTC)

    @classmethod
    def _validated_values(cls, response: list[dict[str, Any]], first: date, last: date, /) -> IntervalValues:
        """Reject gaps, conflicting duplicates, substitutions and changed units."""
        msg: str
        if len(response) != (last - first).days + 1:
            msg = "Unexpected consumption profile count"
            raise ValueError(msg)
        if any(
            profile["unit"].upper() != UnitOfEnergy.KILO_WATT_HOUR.upper()
            or profile["sum"]["unit"].upper() != UnitOfEnergy.KILO_WATT_HOUR.upper()
            or profile["granularity"] != Granularity.QUARTER_OF_AN_HOUR.value
            for profile in response
        ):
            msg = "Unexpected profile unit or granularity"
            raise ValueError(msg)
        values: IntervalValues = {}
        for item in [item for profile in response for item in profile["profileValues"]]:
            stamp: datetime = datetime.fromisoformat(item["datetime"])
            raw: Any = item["value"]
            if (
                stamp.tzinfo is None
                or isinstance(raw, bool)
                or not isinstance(raw, float | int)
                or not math.isfinite(raw)
                or raw < 0
                or item["status"] != ProfileValueStatus.VALID.value
            ):
                msg = "Invalid interval"
                raise ValueError(msg)
            stamp = stamp.astimezone(UTC)
            value: float = float(raw)
            if stamp in values and values[stamp] != value:
                msg = "Conflicting duplicate interval"
                raise ValueError(msg)
            values[stamp] = value
        expected: set[datetime] = set()
        cursor: datetime = cls._midnight_utc(first)
        end: datetime = cls._midnight_utc(last + timedelta(days=1))
        while cursor < end:
            expected.add(cursor)
            cursor += timedelta(minutes=15)
        if set(values) != expected:
            msg = "Incomplete or misaligned consumption profile"
            raise ValueError(msg)
        portal_sum: float = sum(float(profile["sum"]["value"]) for profile in response)
        if not math.isfinite(portal_sum) or not math.isclose(sum(values.values()), portal_sum, abs_tol=0.00001):
            msg = "Profile total mismatch"
            raise ValueError(msg)
        return values

    @staticmethod
    def _hourly_statistics(values: IntervalValues, baseline: float, /) -> list[StatisticData]:
        """Sum four intervals per UTC hour; repeated imports replace identical hours."""
        hourly: dict[datetime, float] = {}
        for stamp, value in sorted(values.items()):
            hour: datetime = stamp.replace(minute=0)
            hourly[hour] = hourly.get(hour, 0.0) + value
        result: list[StatisticData] = []
        total: float = baseline
        for hour, value in hourly.items():
            total += value
            result.append({"start": hour, "state": total, "sum": total})
        return result
