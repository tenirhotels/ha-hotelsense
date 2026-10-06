# Hotel Sense

**Powered by Omada.** Presence and occupancy intelligence for Tenir Hotel on Home Assistant, built on a fork of [zachcheatham/ha-omada](https://github.com/zachcheatham/ha-omada) (Omada API client preserved). Domain: `hotel_sense`.


## Stage A: room presence (Присутствие по номерам)

> Wi-Fi shows **devices, not people**. "5 minutes of cleaning" is an indirect
> signal; random (private) MACs can inflate device counts. Treat the dashboard
> as "something to check", not as proof.

### Setup (once)

The integration's menus, entity names and states follow each user's interface
language (Profile → Language); English and Russian are included. After an
update, reload the browser page (Ctrl+F5) so new texts are picked up.

Hotel Sense adds only the **controller** and the **access points** as devices.
Connected clients (phones, TVs, air conditioners …) are read directly for
presence and get no devices; switches and gateways are skipped. Per-client
devices/entities can be enabled for diagnostics in Configure → Omada polling
and entities.

1. **Areas.** Room = Home Assistant Area (`Room 01` … `Room 10`, `Admin House`).
2. **AP → Area.** When the integration is added, Home Assistant shows the
   *Name and assign* dialog: pick the Area for each access point there
   (e.g. `WF00` → Admin House, `WF01` → Room 01 … `WF10` → Room 10).
   Later changes: Settings → Devices → the AP → Area.

   Optional, for checking or bulk changes: `hotel_sense.ap_area_report`
   (Developer tools → Actions, "return response") lists APs **without an Area**;
   with a MAC → room table it also flags **mismatches** and table MACs that are
   **not found**. `hotel_sense.assign_ap_areas` sets Areas from such a table
   (APs are matched by **MAC**, not by their name in Omada):

   ```yaml
   action: hotel_sense.assign_ap_areas
   data:
     csv: |
       mac,area
       AA-BB-CC-DD-EE-01,Room 01
       aa:bb:cc:dd:ee:00,Admin House
   ```
3. **Fixed and employee devices.** Settings → Devices & services → Hotel Sense →
   Configure → *Fixed and employee devices*: add / edit / delete, import and
   export CSV (`mac,name,category,owner,note,room,device_type`, category `fixed` or `employee`;
   `owner` = employee name, `note` = department/role, `room` = where equipment is
   installed, reference only: the location always comes from the AP).
   MACs are accepted in any notation (`aa:bb:..`, `AA-BB-..`, `aabb.ccdd.eeff`).
   Same via the `hotel_sense.import_devices` / `hotel_sense.export_devices` actions.
   The list is stored in `.storage/hotel_sense.devices` (never in this repo).
4. **Presence options** (Configure → *Room presence*): disconnect timeout
   (default 5 min), roaming debounce (default 30 s), minimum RSSI to move a
   device between rooms (off by default, tune during the pilot), and the list
   of common areas (no status / no violations; default: every Area whose name
   does not start with "Room"/"Номер").
5. **Dashboard.** Paste [`dashboards/hotel_sense_rooms.yaml`](dashboards/hotel_sense_rooms.yaml)
   (Russian) or [`dashboards/hotel_sense_rooms_en.yaml`](dashboards/hotel_sense_rooms_en.yaml) (English)
   into a new dashboard (Raw configuration editor). If your Area IDs are not
   `room_01`…`room_10` / `admin_house`, regenerate it:
   `python scripts/generate_dashboard.py --rooms <area ids> --common <area ids>`.

### Exely PMS: check-in / check-out webhook

Hotel Sense receives Exely webhooks at `https://<your HA>/api/webhook/<id>` and
sets the room status from check-in / check-out events.

1. Hotel Sense → Configure → *Exely PMS (check-in / check-out)* shows the full
   URL and the API key (both generated once, random).
2. In Exely: Настройка гостиницы → Подключение API → your connection → Вебхуки:
   «Использование вебхуков» = Да, URL from step 1, authentication «API-ключ»
   with the key from step 1, events: check-in, check-out and their
   cancellations (a cancelled check-in sets the room back to checked out, a
   cancelled check-out back to checked in). Save.
3. Rooms are matched automatically by name or number (Exely `Room 01`, `01` or
   `1` → Area `Room 01`) only when that is unambiguous. Other numbering (e.g.
   `101`) needs lines like `101 = Room 01` in the room mapping on the same page.
   Exely requires `https://`: if the URL shows `http://`, set the Internet URL to
   `https://…` in HA → Settings → System → Network.

Requests without the right `API-KEY` header are rejected (401). Everything else
is answered with 200 so Exely does not retry. An event is applied only if it is
clearly a check-in or a check-out and the room is found; the result is in
`sensor.hotel_sense_exely_last_event` (`applied` / `partial` / `unmatched` /
`invalid`). The last 20 raw payloads are kept in memory and can be downloaded
via the integration's *Download diagnostics* — they contain guest data, mask it
before sharing. The manual status select stays as a fallback.

If the key or the address leaks, tick *Generate a new API key* and/or *Generate
a new webhook address* in the same settings page: the old one stops working at
once and the form shows the new value to enter in Exely.

### Entities per room (`room_01` = HA area ID)

| Entity | Meaning |
|---|---|
| `select.room_01_status` | Room status: `checked_in` / `checked_out`; manual for now, set by the Exely PMS check-in/check-out webhook in Stage D; restored after restart |
| `binary_sensor.room_01_guest_presence` | Guest or unknown Wi-Fi device in the room |
| `binary_sensor.room_01_employee_presence` | Employee device in the room |
| `binary_sensor.room_01_violation` | Possible violation (red on the dashboard) |
| `sensor.room_01_guest_devices` / `_employee_devices` / `_fixed_devices` | Device counts; attribute `devices` lists MAC + name, `random_macs` counts private MACs |
| `sensor.room_01_state` | Result of the rules below; attribute `data_stale` is `true` while the controller is unreachable |
| `sensor.hotel_sense_misplaced_devices` | Hotel-wide double check: fixed devices with a `room` seen in another room (swapped AP Areas, neighbouring AP, device moved); attribute `devices` lists name, expected and seen room |

Common areas (Admin House) get only presence and counts.

**Connection.** Each device in the `devices` attribute also shows where it is
connected: `ap` (access point name), `ssid`, `rssi` (dBm), `last_seen` and
`connected` (false while a device that left is kept in the room for the
disconnect timeout; the values are then the last known ones).

**Device type.** Each device in the `devices` attribute has a `type` (phone,
tablet, computer, watch, tv, appliance, printer, pos, personal, other, unknown)
and its `type_source`, taken from the first source that knows it: the
`device_type` column of your device list → Omada's own classification → the
hostname (`iPhone`, `Galaxy`…) → a private (random) MAC, which only personal
devices use (`personal` = phone / tablet / laptop) → `unknown`. The `types`
attribute counts them, and the dashboard shows e.g. "Гости: 3 (телефон ×2,
личное устройство ×1)".

### Rules

| Status | Seen | `sensor.*_state` |
|---|---|---|
| Checked out | guest / unknown device | `violation` (red) |
| Checked out | only employee devices | `staff_visit` (not a violation; in the logbook) |
| Checked out | only fixed devices / nothing | `empty` |
| Checked in | anything | `checked_in` |

Cleaning is not a status: it will be derived from staff presence (Stage C) and,
later, compared with the housekeeping status from Exely.

Every change of a room state also fires the `hotel_sense_room_state_changed` event.

### How a device is placed in a room

* Only Wi-Fi clients count; the room is the Area of the AP the client is on.
  Client tracking must stay enabled (Omada options, on by default). The tracker
  SSID filter does not limit presence: devices on every SSID are placed.
* A device that disappears stays in the room for the disconnect timeout.
* Roaming to an AP of another room is accepted after the debounce.
* With a minimum RSSI set, weaker signals never move a device (neighbouring AP).
* When the controller is unreachable, the last picture is kept (no mass "empty").
* Random MACs are not merged automatically.

---

# TP-Link Omada Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Integrate your TP-Link Omada SDN Controller with Home Assistant. This custom component allows you to monitor network clients and Omada devices, control aspects of your network, and leverage Omada data within your automations.

This integration is designed to be installed via [HACS](https://hacs.xyz/).

## Features

### Client Monitoring & Management

* **Device Tracker:** Presence detection for both wired and wireless clients.
  * Attributes include: IP, MAC, hostname, connection type (wired/wireless), connected AP, SSID, signal strength, etc.
  * Configurable disconnect timeout for marking clients as away.
* **Sensors:**
  * Total Downloaded & Uploaded data.
  * Current RX/TX Activity.
  * RSSI & SNR for wireless clients.
  * Uptime.
  * Power Save status.
* **Controls:**
  * Block/Unblock clients.
  * Reconnect wireless clients.
* **Filtering:** Track clients only on specific SSIDs.

### Omada Device (APs, Gateways, Switches) Monitoring & Management

* **Device Tracker:** Presence detection for Omada network devices.
* **Sensors:**
  * Total Downloaded & Uploaded data.
  * Current RX/TX Activity.
  * CPU & Memory Usage.
  * Uptime.
  * Connected client counts (total, 2.4GHz, 5GHz, 6GHz, guests, users).
  * AP Radio Utilization: TX/RX utilization and interference levels for 2.4GHz, 5GHz, and 6GHz bands.
* **Controls (Primarily for Access Points):**
  * Enable/Disable 2.4GHz, 5GHz, and 6GHz radios (switches).
  * Enable/Disable individual SSIDs per AP (switches).
* **Firmware Updates:**
  * Update entity to show available firmware, current version, and trigger upgrades.
  * Release notes for pending updates.

### Controller Features

* **Sensors:**
  * Total number of connected clients across the site.
* **Controls:**

### Configurability

* Granular control over which types of entities are created (e.g., disable bandwidth sensors if not needed).
* Adjustable scan intervals for main data and detailed data.

### Comparison with Official Home Assistant Support

Home Assistant now includes an official core integration for TP-Link Omada SDN. Here's a comparison to help you choose based on information from the official Home Assistant Wiki as of May 2025:

| Feature | Official Integration | Custom |
| :-------------------------------- | :------------------------------------------------------------- | :------------------------------------------------------------- |
| **Installation** | Built-in, configured via Devices & Services. | Requires HACS, then configured via Devices & Services. |
| **Controller Version Support** | 5.1.0 and later. | 4.4.8+ and v5.x.x |
| **Device Support (General)** | Basic status, CPU/Memory, Firmware updates for all devices. | Similar, plus device trackers for Omada hardware. |
| **Network Switch Control** | PoE per-port enable/disable. | Currently no specific per-port PoE exposed (focuses on APs/Clients). |
| **Internet Gateway Control** | WAN/LAN connectivity, Online detection, Connect/Disconnect WAN. | Limited gateway-specific controls (focuses on APs/Clients). |
| **Access Point Control** | Not explicitly detailed in official docs beyond basic status. | Radio (2G/5G/6G) on/off, SSID on/off per AP. |
| **Client Device Tracking** | Tracks Wi-Fi clients (disabled by default). | Tracks wired and wireless clients. |
| **Client Information** | Basic tracking for presence. | IP, MAC, hostname, AP, SSID, signal, etc. |
| **Client Sensors** | Not detailed. | Download/Upload, RX/TX rate, RSSI, SNR, Uptime, Power Save. |
| **Client Controls** | Not detailed. | Block/Unblock switch, Reconnect button. |
| **Client Filtering** | All known Wi-Fi clients. | SSID-based filtering for wireless clients. |
| **AP Radio/Bandwidth Sensors** | Not detailed. | Client counts per band, radio utilization (TX/RX/Interference). |
| **WLAN Optimization Control** | Not detailed. | Binary sensor for status, button to start. |
| **Configuration Granularity** | Primarily site selection. | Toggle individual sensor/switch types for clients and devices, adjust scan intervals, disconnect timeouts. |
| **Data Update Frequency** | Default HA polling, client refresh via service call. | Configurable scan intervals for basic and detailed data. |
| **Cloud Controller Support** | No. | No. |

You can potentially run both integrations if they target different aspects or if you are migrating, but be mindful of potential API rate limiting on your Omada Controller.

If it ever becomes the case that the official integration satifies my own personal needs, I will be switching to it and archiving this repo. Until then, this will continue to be maintained and new features added as time allows.

## Prerequisites

* A running Home Assistant instance.
* [HACS (Home Assistant Community Store)](https://hacs.xyz/) installed.
* A TP-Link Omada SDN Controller (Software* or Hardware Controller). Ensure it's accessible from your Home Assistant instance network-wise.

## Installation

1. Ensure HACS is installed.
2. Open HACS in Home Assistant (usually in the sidebar).
3. Go to **Integrations**.
4. Click the 3-dots menu in the top right and select **Custom Repositories**.
    * URL: `https://github.com/zachcheatham/ha-omada`
    * Category: `Integration`
    * Click **ADD**.
5. Search for "TP-Link Omada" in HACS and click **INSTALL**.
6. Restart Home Assistant.

## Configuration

1. Go to **Settings > Devices & Services** in Home Assistant.
2. Click the **+ ADD INTEGRATION** button (bottom right).
3. Search for "TP-Link Omada" and select it.
4. You will be prompted for the following information:
    * **URL:** The full URL to access your Omada Controller (e.g., `https://omada.yourdomain.com` or `http://192.168.1.10`).
    * **Site:** The name of the Omada site you want to integrate (default is "Default").
    * **Username:** Your Omada Controller username.
    * **Password:** Your Omada Controller password.
    * **Verify SSL:** Check this if your controller uses a valid SSL certificate. Uncheck for self-signed certificates or HTTP.
5. Click **SUBMIT**.

    *Tip: If the integration is encountering an API or related error during configuration, ensure your site name is correct!*

### Options

After successful setup, you can further customize the integration's behavior:

1. Go to **Settings > Devices & Services**.
2. Find your TP-Link Omada integration entry and click **CONFIGURE**.
3. You can adjust:
    * **Scan Intervals:** How frequently to poll the controller for basic and detailed updates.
    * **Tracking:** Enable/disable tracking for clients or Omada devices.
    * **Client Entity Options:** Toggle specific sensors (bandwidth, uptime) and controls (block switch) for clients. Set disconnect timeout and SSID filters.
    * **Device Entity Options:** Toggle specific sensors (bandwidth, statistics, client counts, radio utilization) and controls for Omada devices.

## Supported Controller Versions

This integration is tested and developed against various Omada Controller versions. Generally, it supports:

* Omada Controller v5.x (including the latest 5.13.x versions).

Please note that TP-Link may introduce API changes in new controller versions. If you encounter issues, check the [CHANGELOG.md](CHANGELOG.md) or open an issue.
Additionally, this integration is known to not be compatible with the cloud version of Omada SDN.

## Future Features

* More comprehensive support for Omada switches and gateways.
* Individual client-based filtering for entity creation (beyond SSID).
* Additional SDN and site-wide controls and statistics.
* PoE Controls

## Troubleshooting

* **Connectivity Issues:** Ensure your Home Assistant instance can reach the Omada Controller's IP address and port. Ensure "Verify SSL" is set appropriately.
* **API Errors:** Double-check your URL, username, password, and site name.
* **Entities Not Appearing:** Verify the options in the integration's configuration to ensure the desired entity types are enabled. Check Home Assistant logs for errors.
* For other issues, please check the Home Assistant logs and [open an issue](https://github.com/zachcheatham/ha-omada/issues) on GitHub with relevant log details.

## Contributing

Contributions are welcome! Please feel free to fork the repository, make your changes, and submit a pull request.
