import logging
from inspect import iscoroutinefunction
from datetime import datetime, date, time
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Callable

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CURRENCY_EURO,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from .const import DOMAIN
from .coordinator import OctopusCoordinator
from .runtime import OctopusSpainConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OctopusSpainConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_config_entry_first_refresh()

    sensors = []
    accounts = list(coordinator.data)
    single_account = len(accounts) == 1
    for account in accounts:
        sensors.append(
            OctopusWallet(account, "solar_wallet", "Solar Wallet", coordinator, single_account)
        )
        sensors.append(
            OctopusWallet(
                account, "octopus_credit", "Octopus Credit", coordinator, single_account
            )
        )
        sensors.append(OctopusInvoice(account, coordinator, single_account))
    for account in accounts:
        importer = OctopusConsumptionStatisticsImporter(
            hass=hass,
            coordinator=coordinator,
            account=account,
            single=single_account,
        )
        await importer.async_setup()
        entry.async_on_unload(importer.close)

    async_add_entities(sensors)


class OctopusWallet(CoordinatorEntity[OctopusCoordinator], SensorEntity):

    def __init__(
        self,
        account: str,
        key: str,
        name: str,
        coordinator: OctopusCoordinator,
        single: bool,
    ) -> None:
        super().__init__(coordinator=coordinator)
        self._state = None
        self._key = key
        self._account = account
        self._attrs: Mapping[str, Any] = {}
        self._attr_name = name if single else f"{name} ({account})"
        self._attr_unique_id = f"{key}_{account}"
        self.entity_description = SensorEntityDescription(
            key=f"{key}_{account}",
            icon="mdi:piggy-bank-outline",
            native_unit_of_measurement=CURRENCY_EURO,
            state_class=SensorStateClass.MEASUREMENT
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._state = self.coordinator.data[self._account][self._key]
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType:
        return self._state


class OctopusInvoice(CoordinatorEntity[OctopusCoordinator], SensorEntity):

    def __init__(self, account: str, coordinator: OctopusCoordinator, single: bool) -> None:
        super().__init__(coordinator=coordinator)
        self._state = None
        self._account = account
        self._attrs: Mapping[str, Any] = {}
        self._attr_name = "Última Factura Octopus" if single else f"Última Factura Octopus ({account})"
        self._attr_unique_id = f"last_invoice_{account}"
        self.entity_description = SensorEntityDescription(
            key=f"last_invoice_{account}",
            icon="mdi:currency-eur",
            native_unit_of_measurement=CURRENCY_EURO,
            state_class=SensorStateClass.MEASUREMENT
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data[self._account]['last_invoice']
        self._state = data['amount']
        self._attrs = {
            'Inicio': data['start'],
            'Fin': data['end'],
            'Emitida': data['issued']
        }
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType:
        return self._state

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return self._attrs

class OctopusConsumptionStatisticsImporter:
    """Process coordinator data to feed historical consumption into statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: OctopusCoordinator,
        account: str,
        single: bool,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._account = account
        safe_account = slugify(account)
        self._statistic_id = f"{DOMAIN}:energy_consumption_{safe_account}"
        self._name = "Consumo Electrico" if single else f"Consumo Electrico ({account})"
        self._remove_listener: Callable[[], None] | None = None

    async def async_setup(self) -> None:
        """Run an initial sync and subscribe to coordinator updates."""
        await self._async_process_update()
        self._remove_listener = self._coordinator.async_add_listener(self._schedule_update)

    def close(self) -> None:
        """Unsubscribe from coordinator updates."""
        if self._remove_listener:
            self._remove_listener()
            self._remove_listener = None

    def _schedule_update(self) -> None:
        """Schedule processing for the latest coordinator data."""
        self._hass.async_create_task(self._async_process_update())

    async def _async_process_update(self) -> None:
        prefix = f"OctopusConsumptionStats[{self._account}]"
        try:
            measurements = self._coordinator.data[self._account].get("hourly_consumption", [])
            meas_count = len(measurements) if isinstance(measurements, list) else "unknown"
            _LOGGER.debug(
                "%s: fetched hourly measurements count=%s type=%s",
                prefix,
                meas_count,
                type(measurements).__name__,
            )
            if not measurements:
                _LOGGER.debug("%s: no hourly consumption data available to import", prefix)
                return

            _LOGGER.debug(
                "%s: requesting last statistics statistic_id=%s",
                prefix,
                self._statistic_id,
            )
            last = await get_instance(self._hass).async_add_executor_job(
                get_last_statistics, self._hass, 1, self._statistic_id, True, set()
            )
            _LOGGER.debug("%s: last statistics response=%s", prefix, last)

            last_points = last.get(self._statistic_id)
            last_start = None
            last_sum = 0.0
            if last_points:
                last_point = last_points[-1]

                last_start_raw = last_point.get("start")
                if isinstance(last_start_raw, (int, float)):
                    last_start = dt_util.utc_from_timestamp(last_start_raw)
                elif isinstance(last_start_raw, str):
                    parsed_start = dt_util.parse_datetime(last_start_raw)
                    if parsed_start is not None:
                        last_start = dt_util.as_utc(parsed_start)
                elif isinstance(last_start_raw, datetime):
                    if last_start_raw.tzinfo is None:
                        last_start = last_start_raw.replace(tzinfo=dt_util.UTC)
                    else:
                        last_start = dt_util.as_utc(last_start_raw)

                last_sum_raw = last_point.get("sum")
                if last_sum_raw is None:
                    last_sum_raw = last_point.get("state")
                try:
                    last_sum = float(last_sum_raw or 0.0)
                except (TypeError, ValueError):
                    last_sum = 0.0

                if last_point.get("sum") is None and last_point.get("state") is None:
                    _LOGGER.debug(
                        "%s: last statistics point lacks sum/state, re-importing from start",
                        prefix,
                    )
                    last_start = None
                    last_sum = 0.0

                _LOGGER.debug(
                    "%s: last statistics point start=%s last_sum=%s",
                    prefix,
                    last_start,
                    last_sum,
                )
            else:
                _LOGGER.debug("%s: no existing statistics found, starting fresh", prefix)

            metadata = {
                "has_mean": False,
                "has_sum": True,
                "name": self._name,
                "source": DOMAIN,
                "statistic_id": self._statistic_id,
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            }
            _LOGGER.debug("%s: filled metadata statistics metadata=%s", prefix, metadata)

            def _parse_dt(dt_str: str):
                dt = dt_util.parse_datetime(dt_str)
                if dt is None:
                    return None
                return dt_util.as_utc(dt)

            parsed_measurements = []
            skip_unparsed = 0
            for m in measurements:
                start_at = m.get("startAt")
                start_utc = _parse_dt(start_at)
                if start_utc is None:
                    skip_unparsed += 1
                    continue
                parsed_measurements.append((start_utc, m))

            _LOGGER.debug(
                "%s: parsed measurements count=%s skipped_unparsed=%s",
                prefix,
                len(parsed_measurements),
                skip_unparsed,
            )

            sorted_meas = sorted(parsed_measurements, key=lambda item: item[0])
            _LOGGER.debug("%s: sorted measurements count=%s", prefix, len(sorted_meas))

            statistics = []
            running_sum = last_sum
            skipped_already_imported = 0
            skipped_invalid_value = 0

            for start_utc, m in sorted_meas:
                if last_start is not None and start_utc <= last_start:
                    skipped_already_imported += 1
                    continue

                try:
                    val = float(Decimal(m["value"]))
                except (InvalidOperation, ValueError, TypeError):
                    skipped_invalid_value += 1
                    continue

                running_sum += val
                _LOGGER.debug(
                    "%s: prepared statistic start=%s value=%s running_sum=%s",
                    prefix,
                    start_utc,
                    val,
                    running_sum,
                )

                statistics.append({
                    "start": start_utc,
                    "state": running_sum,
                    "sum": running_sum,
                })

            _LOGGER.debug(
                "%s: post-filter counts prepared=%s skipped_already_imported=%s skipped_invalid_value=%s",
                prefix,
                len(statistics),
                skipped_already_imported,
                skipped_invalid_value,
            )

            if statistics:
                _LOGGER.debug(
                    "%s: adding external statistics metadata=%s statistics=%s",
                    prefix,
                    metadata,
                    statistics,
                )
                if iscoroutinefunction(async_add_external_statistics):
                    await async_add_external_statistics(self._hass, metadata, statistics)
                else:
                    await self._hass.async_add_executor_job(
                        async_add_external_statistics, self._hass, metadata, statistics
                    )
                _LOGGER.debug(
                    "%s: added %d statistics entries",
                    prefix,
                    len(statistics),
                )
            else:
                _LOGGER.debug("%s: no new statistics to add (running_sum=%s)", prefix, running_sum)

        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("%s: error importing consumption statistics: %s", prefix, err)

