from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    AUTH_OPTIONS,
    CONF_APIKEY,
    CONF_AUTH_TYPE,
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
)
from .lib.octopus_spain import OctopusSpain

_LOGGER = logging.getLogger(__name__)

AUTH_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=AUTH_OPTIONS, mode=SelectSelectorMode.DROPDOWN, multiple=False)
)
API_KEY_SELECTOR = TextSelector(TextSelectorConfig(multiline=False, type=TextSelectorType.PASSWORD))
EMAIL_SELECTOR = TextSelector(TextSelectorConfig(multiline=False, type=TextSelectorType.EMAIL))
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(multiline=False, type=TextSelectorType.PASSWORD))
AUTH_TYPE_CREDENTIALS = AUTH_OPTIONS[0]
AUTH_TYPE_APIKEY = AUTH_OPTIONS[1]


def _auth_schema(default: str | None = None) -> vol.Schema:
    value = default or AUTH_OPTIONS[0]
    return vol.Schema({vol.Required(CONF_AUTH_TYPE, default=value): AUTH_SELECTOR})


def _api_key_schema(default: str | None = None) -> vol.Schema:
    key = vol.Required(CONF_APIKEY, default=default) if default is not None else vol.Required(CONF_APIKEY)
    return vol.Schema({key: API_KEY_SELECTOR})


def _credentials_schema(
    email_default: str | None = None, password_default: str | None = None
) -> vol.Schema:
    email_key = (
        vol.Required(CONF_EMAIL, default=email_default)
        if email_default is not None
        else vol.Required(CONF_EMAIL)
    )
    password_key = (
        vol.Required(CONF_PASSWORD, default=password_default)
        if password_default is not None
        else vol.Required(CONF_PASSWORD)
    )
    return vol.Schema({email_key: EMAIL_SELECTOR, password_key: PASSWORD_SELECTOR})


class PlaceholderHub:
    def __init__(self, email: str, password: str) -> None:
        """Initialize."""
        self.email = email
        self.password = password


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._auth_type: str | None = None
        self._cached_data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            auth_default = self._cached_data.get(CONF_AUTH_TYPE)
            return self.async_show_form(step_id="user", data_schema=_auth_schema(auth_default))

        self._auth_type = user_input[CONF_AUTH_TYPE]
        self._cached_data[CONF_AUTH_TYPE] = self._auth_type

        if self._auth_type == AUTH_TYPE_APIKEY:
            return await self.async_step_apikey()
        return await self.async_step_credentials()

    async def async_step_apikey(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        default_key = self._cached_data.get(CONF_APIKEY)
        schema = _api_key_schema(default_key)

        if user_input is None:
            return self.async_show_form(step_id="apikey", data_schema=schema)

        apikey = user_input[CONF_APIKEY]
        self._cached_data[CONF_APIKEY] = apikey
        self._cached_data.setdefault(CONF_EMAIL, None)
        self._cached_data.setdefault(CONF_PASSWORD, None)
        return await self._validate_and_create_entry(schema, "apikey")

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        email_default = self._cached_data.get(CONF_EMAIL)
        password_default = self._cached_data.get(CONF_PASSWORD)
        schema = _credentials_schema(email_default, password_default)

        if user_input is None:
            return self.async_show_form(step_id="credentials", data_schema=schema)

        self._cached_data[CONF_EMAIL] = user_input[CONF_EMAIL]
        self._cached_data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
        self._cached_data[CONF_APIKEY] = None
        return await self._validate_and_create_entry(schema, "credentials")

    async def _validate_and_create_entry(self, schema: vol.Schema, step_id: str) -> FlowResult:
        auth_type = self._cached_data.get(CONF_AUTH_TYPE, AUTH_TYPE_CREDENTIALS)
        email = self._cached_data.get(CONF_EMAIL)
        password = self._cached_data.get(CONF_PASSWORD)
        apikey = self._cached_data.get(CONF_APIKEY)

        api = OctopusSpain(email, password, apikey)
        if await api.login():
            data = {
                CONF_AUTH_TYPE: auth_type,
                CONF_EMAIL: email,
                CONF_PASSWORD: password,
                CONF_APIKEY: apikey,
            }
            return self.async_create_entry(data=data, title="Octopus Spain")

        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors={"base": "invalid_auth"},
        )


class OptionFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        base = {**config_entry.data, **dict(config_entry.options)}
        self._cached_data: dict[str, Any] = {
            CONF_AUTH_TYPE: base.get(CONF_AUTH_TYPE, AUTH_TYPE_CREDENTIALS),
            CONF_EMAIL: base.get(CONF_EMAIL),
            CONF_PASSWORD: base.get(CONF_PASSWORD),
            CONF_APIKEY: base.get(CONF_APIKEY),
        }

    async def async_step_init(self, user_input=None):
        auth_default = self._cached_data.get(CONF_AUTH_TYPE)
        schema = _auth_schema(auth_default)
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)

        auth_type = user_input[CONF_AUTH_TYPE]
        self._cached_data[CONF_AUTH_TYPE] = auth_type

        if auth_type == AUTH_TYPE_APIKEY:
            return await self.async_step_apikey()
        return await self.async_step_credentials()

    async def async_step_apikey(self, user_input=None):
        default_key = self._cached_data.get(CONF_APIKEY)
        schema = _api_key_schema(default_key)
        if user_input is None:
            return self.async_show_form(step_id="apikey", data_schema=schema)

        self._cached_data[CONF_APIKEY] = user_input[CONF_APIKEY]
        self._cached_data[CONF_EMAIL] = None
        self._cached_data[CONF_PASSWORD] = None
        return await self._validate_and_save(schema, "apikey")

    async def async_step_credentials(self, user_input=None):
        email_default = self._cached_data.get(CONF_EMAIL)
        password_default = self._cached_data.get(CONF_PASSWORD)
        schema = _credentials_schema(email_default, password_default)
        if user_input is None:
            return self.async_show_form(step_id="credentials", data_schema=schema)

        self._cached_data[CONF_EMAIL] = user_input[CONF_EMAIL]
        self._cached_data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
        self._cached_data[CONF_APIKEY] = None
        return await self._validate_and_save(schema, "credentials")

    async def _validate_and_save(self, schema: vol.Schema, step_id: str) -> FlowResult:
        auth_type = self._cached_data.get(CONF_AUTH_TYPE, AUTH_TYPE_CREDENTIALS)
        email = self._cached_data.get(CONF_EMAIL)
        password = self._cached_data.get(CONF_PASSWORD)
        apikey = self._cached_data.get(CONF_APIKEY)

        api = OctopusSpain(email, password, apikey)
        if await api.login():
            data = {
                CONF_AUTH_TYPE: auth_type,
                CONF_EMAIL: email,
                CONF_PASSWORD: password,
                CONF_APIKEY: apikey,
            }
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors={"base": "invalid_auth"},
        )

