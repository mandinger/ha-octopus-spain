import logging
from datetime import timedelta
from datetime import datetime, date, time
from homeassistant.const import ENERGY_KILO_WATT_HOUR
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.util import dt as dt_util
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_get_last_statistics,
)
from decimal import Decimal, InvalidOperation

from .const import DOMAIN
from typing import Mapping, Any

from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity
from .const import (
    CONF_PASSWORD,
    CONF_EMAIL, UPDATE_INTERVAL
)

from homeassistant.const import (
    CURRENCY_EURO,
)

from homeassistant.components.sensor import (
    SensorEntityDescription, SensorEntity, SensorStateClass
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .lib.octopus_spain import OctopusSpain

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]

    sensors = []
    coordinator = OctopusCoordinator(hass, email, password)
    await coordinator.async_config_entry_first_refresh()

    accounts = coordinator.data.keys()
    for account in accounts:
        sensors.append(OctopusWallet(account, 'solar_wallet', 'Solar Wallet', coordinator, len(accounts) == 1))
        sensors.append(OctopusWallet(account, 'octopus_credit', 'Octopus Credit', coordinator, len(accounts) == 1))
        sensors.append(OctopusInvoice(account, coordinator, len(accounts) == 1))
        sensors.append(OctopusConsumption(account, coordinator, len(accounts) == 1))

    async_add_entities(sensors)


class OctopusCoordinator(DataUpdateCoordinator):

    def __init__(self, hass: HomeAssistant, email: str, password: str):
        super().__init__(hass=hass, logger=_LOGGER, name="Octopus Spain", update_interval=timedelta(hours=UPDATE_INTERVAL))
        self._api = OctopusSpain(email, password)
        self._data = {}

    async def _async_update_data(self):
        if await self._api.login():
            self._data = {}
            accounts = await self._api.accounts()
            for account in accounts:
                acc = await self._api.account(account)
                if 'hourly_consumption' not in acc:
                    acc['hourly_consumption'] = await self._api.hourly_consumption(account)
                    self._data[account] = acc
        return self._data


class OctopusWallet(CoordinatorEntity, SensorEntity):

    def __init__(self, account: str, key: str, name: str, coordinator, single: bool):
        super().__init__(coordinator=coordinator)
        self._state = None
        self._key = key
        self._account = account
        self._attrs: Mapping[str, Any] = {}
        self._attr_name = f"{name}" if single else f"{name} ({account})"
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


class OctopusInvoice(CoordinatorEntity, SensorEntity):

    def __init__(self, account: str, coordinator, single: bool):
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

class OctopusConsumption(CoordinatorEntity, SensorEntity):
    def __init__(self, account: str, coordinator, single: bool):
        super().__init__(coordinator=coordinator)
        self._account = account
        self._state = None  # we'll expose the current cumulative sum as state
        self._statistic_id = f"{DOMAIN}:{account}_consumption"
        display_name = "Consumo Eléctrico" if single else f"Consumo Eléctrico ({account})"
        self._attr_name = display_name
        self._attr_unique_id = f"consumption_{account}"
        self.entity_description = SensorEntityDescription(
            key=f"consumption_{account}",
            icon="mdi:lightning-bolt",
            native_unit_of_measurement=ENERGY_KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,  # state shows the cumulative kWh
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Process current coordinator data immediately
        await self._async_process_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        # Coordinator callback must be sync; spawn our async processing
        self.hass.async_create_task(self._async_process_update())

    async def _async_process_update(self) -> None:
        try:
            measurements = self.coordinator.data[self._account].get("hourly_consumption", [])
            if not measurements:
                return

            # Get last stored statistics point to continue from there
            last = await async_get_last_statistics(
                self.hass,
                number_of_stats=1,
                statistic_ids=[self._statistic_id],
                include_start_time=True,
            )
            last_points = last.get(self._statistic_id)
            last_start = None
            last_sum = 0.0
            if last_points:
                last_point = last_points[-1]
                last_start = last_point.get("start")
                last_sum = float(last_point.get("sum") or 0.0)

            # Prepare metadata
            metadata = {
                "has_mean": False,
                "has_sum": True,
                "name": self._attr_name,
                "source": DOMAIN,
                "statistic_id": self._statistic_id,
                "unit_of_measurement": ENERGY_KILO_WATT_HOUR,
            }

            # Sort measurements by start time just in case
            def _parse_dt(dt_str: str):
                # Parse and ensure UTC-aware
                dt = dt_util.parse_datetime(dt_str)
                if dt is None:
                    return None
                return dt_util.as_utc(dt)

            sorted_meas = sorted(
                (m for m in measurements if _parse_dt(m["startAt"]) is not None),
                key=lambda m: _parse_dt(m["startAt"])
            )

            statistics = []
            running_sum = last_sum

            for m in sorted_meas:
                start_utc = _parse_dt(m["startAt"])
                if start_utc is None:
                    continue

                # Skip data we already imported (<= last_start)
                if last_start is not None and start_utc <= last_start:
                    continue

                # Convert value to float safely
                try:
                    val = float(Decimal(m["value"]))
                except (InvalidOperation, ValueError, TypeError):
                    continue

                running_sum += val

                # Each hourly bucket starts at the hour (GraphQL already gives the hour start)
                # Recorder expects a UTC-aware datetime
                statistics.append({
                    "start": start_utc,
                    "state": running_sum,  # state can mirror sum for total_increasing
                    "sum": running_sum,
                })

            if statistics:
                await async_add_external_statistics(self.hass, metadata, statistics)

            # Expose current cumulative sum as entity state
            self._state = running_sum
            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.exception("Error importing consumption statistics: %s", err)

    @property
    def native_value(self) -> StateType:
        return self._state