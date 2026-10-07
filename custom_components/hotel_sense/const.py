from homeassistant.const import Platform

DOMAIN = "hotel_sense"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SENSOR,
]

CONF_SITE = "site"
CONF_SCAN_INTERVAL = "scan_interval"
ATTR_MANUFACTURER = "TP-Link"
ATTR_CONTROLLER_MODEL = "Omada Controller"
CLIENTS = "clients"

# Presence (Stage A)
CONF_PRESENCE_TIMEOUT = "presence_timeout"  # minutes
CONF_ROAMING_DEBOUNCE = "roaming_debounce"  # seconds
CONF_MIN_RSSI = "min_rssi"  # dBm, 0 = filter off
CONF_COMMON_AREAS = "common_areas"  # areas without room status / violations
DEFAULT_PRESENCE_TIMEOUT = 5
DEFAULT_ROAMING_DEBOUNCE = 30
DEFAULT_MIN_RSSI = 0

STORAGE_VERSION = 1
STORAGE_KEY_DEVICES = f"{DOMAIN}.devices"

EVENT_ROOM_STATE_CHANGED = f"{DOMAIN}_room_state_changed"

# Exely PMS webhook (Stage D)
CONF_EXELY_WEBHOOK_ID = "exely_webhook_id"  # entry.data, generated
CONF_EXELY_API_KEY = "exely_api_key"  # entry.data, generated
CONF_EXELY_ROOM_MAP = "exely_room_map"  # options: "101 = Room 01" lines
CONF_EXELY_NEW_KEY = "exely_new_key"  # options-flow checkbox, not stored
CONF_EXELY_NEW_URL = "exely_new_url"  # options-flow checkbox, not stored
EVENT_EXELY = f"{DOMAIN}_exely_event"

# Who set a room status (attribute ``source`` of the status select).
STATUS_SOURCE_MANUAL = "manual"
STATUS_SOURCE_EXELY = "exely"
STATUS_SOURCE_RESTORED = "restored"  # kept from before a restart / reload
