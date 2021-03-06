"""Support for the Abode Security System."""
from asyncio import gather
from copy import deepcopy
from functools import partial
import logging

from abodepy import Abode
from abodepy.exceptions import AbodeException
import abodepy.helpers.timeline as TIMELINE
from requests.exceptions import ConnectTimeout, HTTPError
import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import (
    ATTR_ATTRIBUTION,
    ATTR_DATE,
    ATTR_ENTITY_ID,
    ATTR_TIME,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import Entity

from .const import ATTRIBUTION, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_POLLING = "polling"

DEFAULT_CACHEDB = "./abodepy_cache.pickle"

SERVICE_SETTINGS = "change_setting"
SERVICE_CAPTURE_IMAGE = "capture_image"
SERVICE_TRIGGER = "trigger_quick_action"

ATTR_DEVICE_ID = "device_id"
ATTR_DEVICE_NAME = "device_name"
ATTR_DEVICE_TYPE = "device_type"
ATTR_EVENT_CODE = "event_code"
ATTR_EVENT_NAME = "event_name"
ATTR_EVENT_TYPE = "event_type"
ATTR_EVENT_UTC = "event_utc"
ATTR_SETTING = "setting"
ATTR_USER_NAME = "user_name"
ATTR_VALUE = "value"

ABODE_DEVICE_ID_LIST_SCHEMA = vol.Schema([str])

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Optional(CONF_POLLING, default=False): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

CHANGE_SETTING_SCHEMA = vol.Schema(
    {vol.Required(ATTR_SETTING): cv.string, vol.Required(ATTR_VALUE): cv.string}
)

CAPTURE_IMAGE_SCHEMA = vol.Schema({ATTR_ENTITY_ID: cv.entity_ids})

TRIGGER_SCHEMA = vol.Schema({ATTR_ENTITY_ID: cv.entity_ids})

ABODE_PLATFORMS = [
    "alarm_control_panel",
    "binary_sensor",
    "lock",
    "switch",
    "cover",
    "camera",
    "light",
    "sensor",
]


class AbodeSystem:
    """Abode System class."""

    def __init__(self, abode, polling):
        """Initialize the system."""

        self.abode = abode
        self.polling = polling
        self.devices = []
        self.logout_listener = None


async def async_setup(hass, config):
    """Set up Abode integration."""
    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=deepcopy(conf)
        )
    )

    return True


async def async_setup_entry(hass, config_entry):
    """Set up Abode integration from a config entry."""
    username = config_entry.data.get(CONF_USERNAME)
    password = config_entry.data.get(CONF_PASSWORD)
    polling = config_entry.data.get(CONF_POLLING)

    try:
        cache = hass.config.path(DEFAULT_CACHEDB)
        abode = await hass.async_add_executor_job(
            Abode, username, password, True, True, True, cache
        )
        hass.data[DOMAIN] = AbodeSystem(abode, polling)

    except (AbodeException, ConnectTimeout, HTTPError) as ex:
        _LOGGER.error("Unable to connect to Abode: %s", str(ex))
        return False

    for platform in ABODE_PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(config_entry, platform)
        )

    await setup_hass_events(hass)
    await hass.async_add_executor_job(setup_hass_services, hass)
    await hass.async_add_executor_job(setup_abode_events, hass)

    return True


async def async_unload_entry(hass, config_entry):
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_SETTINGS)
    hass.services.async_remove(DOMAIN, SERVICE_CAPTURE_IMAGE)
    hass.services.async_remove(DOMAIN, SERVICE_TRIGGER)

    tasks = []

    for platform in ABODE_PLATFORMS:
        tasks.append(
            hass.config_entries.async_forward_entry_unload(config_entry, platform)
        )

    await gather(*tasks)

    await hass.async_add_executor_job(hass.data[DOMAIN].abode.events.stop)
    await hass.async_add_executor_job(hass.data[DOMAIN].abode.logout)

    hass.data[DOMAIN].logout_listener()
    hass.data.pop(DOMAIN)

    return True


def setup_hass_services(hass):
    """Home assistant services."""

    def change_setting(call):
        """Change an Abode system setting."""
        setting = call.data.get(ATTR_SETTING)
        value = call.data.get(ATTR_VALUE)

        try:
            hass.data[DOMAIN].abode.set_setting(setting, value)
        except AbodeException as ex:
            _LOGGER.warning(ex)

    def capture_image(call):
        """Capture a new image."""
        entity_ids = call.data.get(ATTR_ENTITY_ID)

        target_devices = [
            device
            for device in hass.data[DOMAIN].devices
            if device.entity_id in entity_ids
        ]

        for device in target_devices:
            device.capture()

    def trigger_quick_action(call):
        """Trigger a quick action."""
        entity_ids = call.data.get(ATTR_ENTITY_ID, None)

        target_devices = [
            device
            for device in hass.data[DOMAIN].devices
            if device.entity_id in entity_ids
        ]

        for device in target_devices:
            device.trigger()

    hass.services.register(
        DOMAIN, SERVICE_SETTINGS, change_setting, schema=CHANGE_SETTING_SCHEMA
    )

    hass.services.register(
        DOMAIN, SERVICE_CAPTURE_IMAGE, capture_image, schema=CAPTURE_IMAGE_SCHEMA
    )

    hass.services.register(
        DOMAIN, SERVICE_TRIGGER, trigger_quick_action, schema=TRIGGER_SCHEMA
    )


async def setup_hass_events(hass):
    """Home Assistant start and stop callbacks."""

    def logout(event):
        """Logout of Abode."""
        if not hass.data[DOMAIN].polling:
            hass.data[DOMAIN].abode.events.stop()

        hass.data[DOMAIN].abode.logout()
        _LOGGER.info("Logged out of Abode")

    if not hass.data[DOMAIN].polling:
        await hass.async_add_executor_job(hass.data[DOMAIN].abode.events.start)

    hass.data[DOMAIN].logout_listener = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, logout
    )


def setup_abode_events(hass):
    """Event callbacks."""

    def event_callback(event, event_json):
        """Handle an event callback from Abode."""
        data = {
            ATTR_DEVICE_ID: event_json.get(ATTR_DEVICE_ID, ""),
            ATTR_DEVICE_NAME: event_json.get(ATTR_DEVICE_NAME, ""),
            ATTR_DEVICE_TYPE: event_json.get(ATTR_DEVICE_TYPE, ""),
            ATTR_EVENT_CODE: event_json.get(ATTR_EVENT_CODE, ""),
            ATTR_EVENT_NAME: event_json.get(ATTR_EVENT_NAME, ""),
            ATTR_EVENT_TYPE: event_json.get(ATTR_EVENT_TYPE, ""),
            ATTR_EVENT_UTC: event_json.get(ATTR_EVENT_UTC, ""),
            ATTR_USER_NAME: event_json.get(ATTR_USER_NAME, ""),
            ATTR_DATE: event_json.get(ATTR_DATE, ""),
            ATTR_TIME: event_json.get(ATTR_TIME, ""),
        }

        hass.bus.fire(event, data)

    events = [
        TIMELINE.ALARM_GROUP,
        TIMELINE.ALARM_END_GROUP,
        TIMELINE.PANEL_FAULT_GROUP,
        TIMELINE.PANEL_RESTORE_GROUP,
        TIMELINE.AUTOMATION_GROUP,
    ]

    for event in events:
        hass.data[DOMAIN].abode.events.add_event_callback(
            event, partial(event_callback, event)
        )


class AbodeDevice(Entity):
    """Representation of an Abode device."""

    def __init__(self, data, device):
        """Initialize Abode device."""
        self._data = data
        self._device = device

    async def async_added_to_hass(self):
        """Subscribe to device events."""
        self.hass.async_add_job(
            self._data.abode.events.add_device_callback,
            self._device.device_id,
            self._update_callback,
        )

    async def async_will_remove_from_hass(self):
        """Unsubscribe from device events."""
        self.hass.async_add_job(
            self._data.abode.events.remove_all_device_callbacks, self._device.device_id
        )

    @property
    def should_poll(self):
        """Return the polling state."""
        return self._data.polling

    def update(self):
        """Update device and automation states."""
        self._device.refresh()

    @property
    def name(self):
        """Return the name of the device."""
        return self._device.name

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            "device_id": self._device.device_id,
            "battery_low": self._device.battery_low,
            "no_response": self._device.no_response,
            "device_type": self._device.type,
        }

    @property
    def unique_id(self):
        """Return a unique ID to use for this device."""
        return self._device.device_uuid

    @property
    def device_info(self):
        """Return device registry information for this entity."""
        return {
            "identifiers": {(DOMAIN, self._device.device_id)},
            "manufacturer": "Abode",
            "name": self._device.name,
            "device_type": self._device.type,
        }

    def _update_callback(self, device):
        """Update the device state."""
        self.schedule_update_ha_state()


class AbodeAutomation(Entity):
    """Representation of an Abode automation."""

    def __init__(self, data, automation, event=None):
        """Initialize for Abode automation."""
        self._data = data
        self._automation = automation
        self._event = event

    async def async_added_to_hass(self):
        """Subscribe Abode events."""
        if self._event:
            self.hass.async_add_job(
                self._data.abode.events.add_event_callback,
                self._event,
                self._update_callback,
            )

    @property
    def should_poll(self):
        """Return the polling state."""
        return self._data.polling

    def update(self):
        """Update automation state."""
        self._automation.refresh()

    @property
    def name(self):
        """Return the name of the automation."""
        return self._automation.name

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            "automation_id": self._automation.automation_id,
            "type": self._automation.type,
            "sub_type": self._automation.sub_type,
        }

    def _update_callback(self, device):
        """Update the automation state."""
        self._automation.refresh()
        self.schedule_update_ha_state()
