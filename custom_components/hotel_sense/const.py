from homeassistant.const import Platform

DOMAIN = "hotel_sense"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

CONF_SITE = "site"
CONF_SSID_FILTER = "ssid_filter"
CONF_DISCONNECT_TIMEOUT = "disconnect_timeout"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_SCAN_INTERVAL_DETAILS = "scan_interval_details"
CONF_TRACK_CLIENTS = "track_clients"
CONF_TRACK_DEVICES = "track_devices"
CONF_ENABLE_CLIENT_BANDWIDTH_SENSORS = "enable_client_bandwidth_sensors"
CONF_ENABLE_CLIENT_UPTIME_SENSORS = "enable_client_uptime_sensors"
CONF_ENABLE_CLIENT_BLOCK_SWITCH = "enable_client_block_switch"
CONF_ENABLE_DEVICE_BANDWIDTH_SENSORS = "enable_device_bandwidth_sensors"
CONF_ENABLE_DEVICE_RADIO_UTILIZATION_SENSORS = "enable_device_radio_utilization_sensors"
CONF_ENABLE_DEVICE_CONTROLS = "enable_device_controls"
CONF_ENABLE_DEVICE_STATISTICS_SENSORS = "enable_device_statistics_sensors"
CONF_ENABLE_DEVICE_CLIENTS_SENSORS = "enable_device_clients_sensors"
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
