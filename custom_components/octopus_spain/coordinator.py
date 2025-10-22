from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import UPDATE_INTERVAL
from .lib.octopus_spain import OctopusSpain

_LOGGER = logging.getLogger(__name__)


class OctopusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """DataUpdateCoordinator for Octopus Spain accounts."""

    def __init__(self, hass: HomeAssistant, email: str | None, password: str | None, api_key: str | None) -> None:
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name="Octopus Spain",
            update_interval=timedelta(hours=UPDATE_INTERVAL),
        )
        self._api = OctopusSpain(email, password, api_key)
        self._data: dict[str, Any] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        if await self._api.login():
            self._data = {}
            accounts = await self._api.accounts()
            for account in accounts:
                acc = await self._api.account(account)
                if "hourly_consumption" not in acc:
                    acc["hourly_consumption"] = await self._api.hourly_consumption(account)
                self._data[account] = acc
        return self._data
