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

# Options of Hotel Sense <= 0.2 (ha-omada entities), no longer used.
LEGACY_OPTIONS = {
    "scan_interval_details", "track_clients", "track_devices", "ssid_filter",
    "disconnect_timeout", "enable_client_bandwidth_sensors", "enable_client_uptime_sensors",
    "enable_client_block_switch", "enable_device_bandwidth_sensors",
    "enable_device_radio_utilization_sensors", "enable_device_controls",
    "enable_device_statistics_sensors", "enable_device_clients_sensors",
}

# Presence (Stage A)
CONF_PRESENCE_TIMEOUT = "presence_timeout"  # minutes
CONF_ROAMING_DEBOUNCE = "roaming_debounce"  # seconds
CONF_MIN_RSSI = "min_rssi"  # dBm, 0 = filter off
CONF_COMMON_AREAS = "common_areas"  # areas without room status / violations
CONF_SLEEP_TIMEOUT = "sleep_timeout"  # minutes, 0 = same as presence_timeout
DEFAULT_PRESENCE_TIMEOUT = 5
DEFAULT_ROAMING_DEBOUNCE = 30
DEFAULT_MIN_RSSI = 0
DEFAULT_SLEEP_TIMEOUT = 0

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
# Exely Connect API (room of a booking): entry.data, entered in the options
CONF_EXELY_CLIENT_ID = "exely_client_id"
CONF_EXELY_CLIENT_SECRET = "exely_client_secret"
CONF_EXELY_PROPERTY_ID = "exely_property_id"

# History database (MariaDB add-on): connection in entry.data, retention in options
CONF_DB_HOST = "db_host"
CONF_DB_PORT = "db_port"
CONF_DB_USERNAME = "db_username"
CONF_DB_PASSWORD = "db_password"
CONF_DB_NAME = "db_name"
DB_KEYS = (CONF_DB_HOST, CONF_DB_PORT, CONF_DB_USERNAME, CONF_DB_PASSWORD, CONF_DB_NAME)
CONF_DB_RETENTION = "db_retention_months"
DEFAULT_DB_HOST = "core-mariadb"
DEFAULT_DB_PORT = 3306
DEFAULT_DB_NAME = "hotel_sense"

# Omada controller webhook (instant refresh)
CONF_OMADA_WEBHOOK_ID = "omada_webhook_id"  # entry.data, generated
CONF_OMADA_WEBHOOK_SECRET = "omada_webhook_secret"  # entry.data, generated ("Shard Secret")
CONF_OMADA_NEW_SECRET = "omada_new_secret"  # options-flow checkbox, not stored

# Who set a room status (attribute ``source`` of the status select).
STATUS_SOURCE_MANUAL = "manual"
STATUS_SOURCE_EXELY = "exely"
STATUS_SOURCE_RESTORED = "restored"  # kept from before a restart / reload
