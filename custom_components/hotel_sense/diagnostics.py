"""Config entry diagnostics: recent Exely webhook payloads (to learn their format).

The download contains guest data from Exely: mask names, phones, e-mails and
document numbers before sharing it. Credentials and secrets are redacted.
"""
from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_EXELY_API_KEY, CONF_EXELY_WEBHOOK_ID, CONF_OMADA_WEBHOOK_ID, CONF_OMADA_WEBHOOK_SECRET,
    DOMAIN,
)

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_EXELY_API_KEY, CONF_EXELY_WEBHOOK_ID,
             CONF_OMADA_WEBHOOK_ID, CONF_OMADA_WEBHOOK_SECRET}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    controller = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "omada": {
            "controller_version": controller.hub.version,
            "available": controller.available,
            "access_points": len(controller.access_points),
            "access_points_offline": sum(1 for ap in controller.access_points.values() if not ap.online),
            "connected_clients": len(controller.clients),
            "wireless_clients": sum(1 for c in controller.clients.values() if c.wireless),
            "power_save_clients": sum(1 for c in controller.clients.values() if c.power_save),
        },
        "omada_webhook": controller.omada_webhook.diagnostics(),
        "exely_recent_events": list(controller.exely.recent),
        "rooms": {a: {"name": r.name, "status": r.status, "state": r.state}
                  for a, r in controller.presence.rooms.items()},
    }
