# Copyright 2026 Michael Hacker
# SPDX-License-Identifier: Apache-2.0
"""Exercise profile imports through Home Assistant and mocked provider HTTP."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import Final
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant.components.recorder.common import async_recorder_block_till_done

from tests import next_update
from tests import setup_integration
from tests.const import API_URL
from tests.const import NOW
from tests.test_profile_statistics_data import ACCOUNT
from tests.test_profile_statistics_data import CORRECTED_COST
from tests.test_profile_statistics_data import DAY
from tests.test_profile_statistics_data import HOURS
from tests.test_profile_statistics_data import INITIAL_COST
from tests.test_profile_statistics_data import STATISTIC_ID
from tests.test_profile_statistics_data import profile_catalog
from tests.test_profile_statistics_data import profile_data

pytestmark: Final = [pytest.mark.usefixtures("recorder_mock"), pytest.mark.freeze_time(NOW)]

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.components.recorder import Recorder
    from homeassistant.components.recorder.models import StatisticData
    from homeassistant.components.recorder.models import StatisticMetaData
    from homeassistant.components.recorder.statistics import StatisticsRow
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant.common import MockConfigEntry

    from tests.conftest import FakeNetzOOEeServiceAPI


async def _statistics(
    hass: HomeAssistant, recorder: Recorder, statistic_id: str = STATISTIC_ID
) -> dict[str, list[StatisticsRow]]:
    """Read persisted results through Recorder's public statistics API."""
    await async_recorder_block_till_done(hass)
    convert_units: bool = True
    return await recorder.async_add_executor_job(get_last_statistics, hass, 1000, statistic_id, convert_units, {"sum"})


def _register(
    fake_api: FakeNetzOOEeServiceAPI, day: date, response: list[dict[str, Any]], *, last_day: date | None = None
) -> None:
    """Register synthetic metadata and interval responses."""
    fake_api.register_auth_request()
    fake_api.register_requests()
    fake_api.aioclient_mock.get(
        f"{API_URL}/v1.0/consumptions/profiles?branch=STROM&activeOnly=true",
        json=profile_catalog(day, last_day=last_day),
    )
    fake_api.profile_responses[(ACCOUNT, "", day.isoformat(), day.isoformat())] = response


@pytest.mark.parametrize("entry_options", [{"import_consumption_profiles": True}])
@pytest.mark.parametrize(
    "case",
    [(DAY, 24), (date(2026, 3, 29), 23), (date(2025, 10, 26), 25)],
    ids=["normal-day", "spring-forward", "fall-back"],
)
async def test_profile_import_measurement_hours(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
    case: tuple[date, int],
) -> None:
    """Store complete hours at measurement time, including both daylight-saving transitions."""
    day, hours = case
    await hass.config.async_update(time_zone="Europe/Vienna")
    _register(fake_api, day, profile_data(day))
    await setup_integration(hass, mock_config_entry)
    await recorder_mock.async_block_till_done()
    result: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert len(result[STATISTIC_ID]) == hours
    assert result[STATISTIC_ID][0]["sum"] == hours


@pytest.mark.parametrize("entry_options", [{"import_consumption_profiles": True}])
async def test_profile_replay_is_idempotent(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
) -> None:
    """Repeated polling replaces the same hours and never doubles consumption."""
    await hass.config.async_update(time_zone="Europe/Vienna")
    _register(fake_api, DAY, profile_data(DAY))
    await setup_integration(hass, mock_config_entry)
    await next_update(hass, freezer)
    result: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock)
    assert len(result[STATISTIC_ID]) == HOURS
    assert result[STATISTIC_ID][0]["sum"] == HOURS


@pytest.mark.parametrize(
    "entry_options",
    [{"import_consumption_profiles": True, "profile_energy_price": 0.25, "profile_energy_price_from": DAY.isoformat()}],
)
async def test_profile_setup_does_not_wait_for_recorder_startup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
) -> None:
    """Load before Recorder can commit, then persist both energy and cost statistics."""
    await hass.config.async_update(time_zone="Europe/Vienna", currency="EUR")
    _register(fake_api, DAY, profile_data(DAY))
    with patch.object(
        recorder_mock,
        "async_block_till_done",
        side_effect=RuntimeError("Recorder cannot commit before Home Assistant starts"),
    ):
        await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    energy: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock)
    costs: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock, STATISTIC_ID + "_energy_cost")
    assert len(energy[STATISTIC_ID]) == HOURS
    assert energy[STATISTIC_ID][0]["sum"] == HOURS
    assert len(costs[STATISTIC_ID + "_energy_cost"]) == HOURS
    assert costs[STATISTIC_ID + "_energy_cost"][0]["sum"] == INITIAL_COST


@pytest.mark.parametrize("entry_options", [{"import_consumption_profiles": True}])
@pytest.mark.parametrize(
    "case",
    [
        ("value", -1),
        ("value", True),
        ("value", "0.25"),
        ("value", float("nan")),
        ("status", "ESTIMATED"),
        ("datetime", "2026-06-26T00:00:00"),
    ],
    ids=["negative", "boolean", "text", "nonfinite", "unconfirmed", "naive-time"],
)
async def test_invalid_profile_preserves_native_entities(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
    case: tuple[str, bool | float | int | str],
) -> None:
    """Invalid profiles do not pollute statistics or stop the existing integration."""
    field, value = case
    await hass.config.async_update(time_zone="Europe/Vienna")
    response: list[dict[str, Any]] = profile_data(DAY)
    response[0]["profileValues"][0][field] = value
    _register(fake_api, DAY, response)
    await setup_integration(hass, mock_config_entry)
    result: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock)
    assert result == {}
    assert mock_config_entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    "entry_options",
    [{"import_consumption_profiles": True, "profile_energy_price": 0.25, "profile_energy_price_from": DAY.isoformat()}],
)
async def test_dated_costs_and_corrections(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
) -> None:
    """Energy and variable cost use the same measurement hours and corrections."""
    await hass.config.async_update(time_zone="Europe/Vienna", currency="EUR")
    response: list[dict[str, Any]] = profile_data(DAY)
    _register(fake_api, DAY, response)
    await setup_integration(hass, mock_config_entry)
    cost_id: str = STATISTIC_ID + "_energy_cost"
    result: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock, cost_id)
    assert len(result[cost_id]) == HOURS
    assert result[cost_id][0]["sum"] == INITIAL_COST
    response[0]["profileValues"][0]["value"] = 0.5
    response[0]["sum"]["value"] = 24.25
    await next_update(hass, freezer, interval=timedelta(hours=4))
    corrected: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock, cost_id)
    assert len(corrected[cost_id]) == HOURS
    assert corrected[cost_id][0]["sum"] == CORRECTED_COST


@pytest.mark.parametrize(
    "entry_options",
    [{"import_consumption_profiles": True, "profile_energy_price": 0.25, "profile_energy_price_from": "2026-06-27"}],
)
async def test_costs_do_not_predate_the_tariff(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
) -> None:
    """A known future price never manufactures historical costs."""
    _register(fake_api, DAY, profile_data(DAY))
    await setup_integration(hass, mock_config_entry)
    cost_id: str = STATISTIC_ID + "_energy_cost"
    result: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock, cost_id)
    assert result == {}
    assert mock_config_entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("entry_options", [{"import_consumption_profiles": True}])
async def test_multi_day_backfill_and_anchored_correction(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
) -> None:
    """Batch individual days, preserve the prefix and replace a correction window."""
    first: date = DAY - timedelta(days=31)
    _register(fake_api, first, profile_data(first), last_day=DAY)
    for offset in range(32):
        day: date = first + timedelta(days=offset)
        fake_api.profile_responses[(ACCOUNT, "", day.isoformat(), day.isoformat())] = profile_data(day)
    await setup_integration(hass, mock_config_entry)
    initial: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock)
    expected_hours: int = 32 * HOURS
    assert len(initial[STATISTIC_ID]) == expected_hours
    assert initial[STATISTIC_ID][0]["sum"] == expected_hours
    corrected: list[dict[str, Any]] = profile_data(DAY)
    corrected[0]["profileValues"][0]["value"] = 0.5
    corrected[0]["sum"]["value"] += 0.25
    fake_api.profile_responses[(ACCOUNT, "", DAY.isoformat(), DAY.isoformat())] = corrected
    await next_update(hass, freezer)
    result: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock)
    assert len(result[STATISTIC_ID]) == expected_hours
    assert result[STATISTIC_ID][0]["sum"] == expected_hours + 0.25
    assert result[STATISTIC_ID][-1] == initial[STATISTIC_ID][-1]


@pytest.mark.parametrize("entry_options", [{"import_consumption_profiles": True}])
@pytest.mark.parametrize(
    "issue",
    ["count", "unit", "sum-unit", "granularity", "gap", "duplicate", "total"],
    ids=[
        "missing-profile",
        "wrong-unit",
        "wrong-total-unit",
        "coarser-data",
        "missing-quarter",
        "conflicting-quarter",
        "wrong-total",
    ],
)
async def test_profile_validation_and_recovery(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    issue: str,
    profile_test_context: tuple[FrozenDateTimeFactory, pytest.LogCaptureFixture],
) -> None:
    """Defer inconsistent profiles, warn once, and recover without duplicate sums."""
    freezer, caplog = profile_test_context
    recorder_mock: Recorder = get_instance(hass)
    caplog.set_level("INFO", logger="custom_components.netzooe_eservice.coordinator")
    response: list[dict[str, Any]] = profile_data(DAY)
    mutations: dict[str, list[dict[str, Any]]] = {
        "count": [],
        "unit": [response[0] | {"unit": "WH"}],
        "sum-unit": [response[0] | {"sum": {"value": HOURS, "unit": "WH"}}],
        "granularity": [response[0] | {"granularity": "DAY"}],
        "gap": [response[0] | {"profileValues": response[0]["profileValues"][1:]}],
        "duplicate": [
            response[0]
            | {"profileValues": response[0]["profileValues"] + [response[0]["profileValues"][0] | {"value": 1}]}
        ],
        "total": [response[0] | {"sum": {"value": HOURS + 1, "unit": "KWH"}}],
    }
    _register(fake_api, DAY, mutations[issue])
    await setup_integration(hass, mock_config_entry)
    await next_update(hass, freezer)
    assert await _statistics(hass, recorder_mock) == {}
    assert caplog.text.count("Consumption profile import deferred") == 1
    fake_api.profile_responses[(ACCOUNT, "", DAY.isoformat(), DAY.isoformat())] = profile_data(DAY)
    await next_update(hass, freezer)
    assert "Consumption profile import recovered" in caplog.text
    result: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock)
    assert result[STATISTIC_ID][0]["sum"] == HOURS


@pytest.mark.parametrize("entry_options", [{"import_consumption_profiles": True}])
async def test_identical_duplicate_does_not_double_energy(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
) -> None:
    """An identical provider duplicate is deduplicated before aggregation."""
    response: list[dict[str, Any]] = profile_data(DAY)
    response[0]["profileValues"].append(dict(response[0]["profileValues"][0]))
    _register(fake_api, DAY, response)
    await setup_integration(hass, mock_config_entry)
    result: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock)
    assert result[STATISTIC_ID][0]["sum"] == HOURS


@pytest.mark.parametrize("entry_options", [{"import_consumption_profiles": True}])
async def test_today_is_not_imported(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
) -> None:
    """Only completed provider days can enter statistics, regardless of HA timezone."""
    _register(fake_api, NOW.date(), profile_data(NOW.date()))
    await setup_integration(hass, mock_config_entry)
    assert await _statistics(hass, recorder_mock) == {}
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def _seed_statistics(hass: HomeAssistant, statistic_id: str, rows: list[StatisticData]) -> None:
    """Seed pre-existing recorder data to exercise import recovery through HA."""
    metadata: StatisticMetaData = {
        "source": "netzooe_eservice",
        "statistic_id": statistic_id,
        "name": "Synthetic existing statistics",
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "unit_class": None,
        "unit_of_measurement": "EUR" if statistic_id.endswith("cost") else "kWh",
    }
    async_add_external_statistics(hass, metadata, rows)
    await get_instance(hass).async_block_till_done()


@pytest.mark.parametrize(
    "entry_options",
    [{"import_consumption_profiles": True, "profile_energy_price": 0.25, "profile_energy_price_from": "2026-06-01"}],
)
@pytest.mark.parametrize(
    "scenario",
    ["missing-energy", "invalid-energy", "missing-cost"],
    ids=["backfill-without-anchor", "reject-invalid-anchor", "defer-missing-cost-anchor"],
)
async def test_existing_statistics_anchors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
    scenario: str,
) -> None:
    """Missing energy anchors trigger full backfill; corrupt/cost gaps defer safely."""
    first: date = DAY - timedelta(days=10)
    _register(fake_api, first, profile_data(first), last_day=DAY)
    for offset in range(11):
        day: date = first + timedelta(days=offset)
        fake_api.profile_responses[(ACCOUNT, "", day.isoformat(), day.isoformat())] = profile_data(day)
    latest: datetime = datetime.fromisoformat(profile_data(DAY)[0]["profileValues"][-1]["datetime"]).replace(minute=0)
    anchor: datetime = datetime.fromisoformat(
        profile_data(DAY - timedelta(days=6))[0]["profileValues"][0]["datetime"]
    ) - timedelta(hours=1)
    rows: dict[str, list[StatisticData]] = {
        "missing-energy": [{"start": latest, "sum": 264}],
        "invalid-energy": [{"start": anchor}, {"start": latest, "sum": 264}],
        "missing-cost": [{"start": anchor, "sum": 96}, {"start": latest, "sum": 264}],
    }
    await _seed_statistics(hass, STATISTIC_ID, rows[scenario])
    await _seed_statistics(hass, STATISTIC_ID + "_energy_cost", [{"start": latest, "sum": 66}])
    await setup_integration(hass, mock_config_entry)
    result: dict[str, list[StatisticsRow]] = await _statistics(hass, recorder_mock)
    expected_rows: dict[str, int] = {"missing-energy": 11 * HOURS, "invalid-energy": 2, "missing-cost": 2}
    assert len(result[STATISTIC_ID]) == expected_rows[scenario]
    expected_sum: int = 11 * HOURS
    assert result[STATISTIC_ID][0]["sum"] == expected_sum
    assert mock_config_entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("entry_options", [{"import_consumption_profiles": True}])
async def test_non_quarter_hour_profiles_are_skipped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
) -> None:
    """A daily-only meter must not be presented as a quarter-hour profile."""
    catalog: list[dict[str, Any]] = profile_catalog(DAY)
    catalog[0]["profiles"][0]["granularity"] = "DAY"
    fake_api.register_auth_request()
    fake_api.register_requests()
    fake_api.aioclient_mock.get(f"{API_URL}/v1.0/consumptions/profiles?branch=STROM&activeOnly=true", json=catalog)
    await setup_integration(hass, mock_config_entry)
    assert await _statistics(hass, recorder_mock) == {}


@pytest.mark.parametrize(
    "entry_options",
    [{"import_consumption_profiles": True, "profile_energy_price": -1, "profile_energy_price_from": DAY.isoformat()}],
)
async def test_invalid_price_preserves_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_api: FakeNetzOOEeServiceAPI,
    recorder_mock: Recorder,
) -> None:
    """Invalid stored options must never produce negative cost statistics."""
    _register(fake_api, DAY, profile_data(DAY))
    await setup_integration(hass, mock_config_entry)
    assert await _statistics(hass, recorder_mock) == {}
    assert mock_config_entry.state is ConfigEntryState.LOADED
