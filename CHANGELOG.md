# Hotel Sense 0.8.0
- Room model: each room (HA Area) has a Hotel Sense record with kind (room / common), number, Exely labels and its check-in status *with its origin*: value, source (manual / exely / restored), changed_at (UTC), booking (Exely) and user_id (manual change). The last change wins. Stored in `.storage/hotel_sense.rooms.<entry>`; the status no longer depends on restoring the select entity
- Configure → Rooms: all rooms as CSV (`room_id;name;number;kind;exely_room_ids`); the common areas (Room presence) and the Exely room mapping edit the same model
- The `common_areas` and `exely_room_map` options are moved into the room model on the first start
- The room status select shows `booking` and `user_id`; a room that becomes a common area loses its status / state / violation entities

# Hotel Sense 0.7.0
- History: Wi-Fi traffic per room, device category and hour (`room_traffic`, from Omada's per-client counters; database schema 2, created automatically)
- Service health entities (diagnostic, on the controller device), for notifications and a system-status card: *Omada controller* (connectivity), *Omada webhook last message* (timestamp, with received / rejected counts), *Exely API* (not set up / OK / error, with the last error) and *History database* (connectivity, with queued / written / last error; only when the database is set up)

# Hotel Sense 0.6.4
- Settings: cleared fields stay cleared. Exely API (client ID), history database (user) and the Exely room mapping were pre-filled as defaults, so Home Assistant put the old value back when a field was emptied - the API / history could not be switched off and the mapping not cleared. They are now suggested values

# Hotel Sense 0.6.3
- `exely_api_probe` also asks the Content API (property) and, with a booking number, the Read Reservation API: shows whether other Exely APIs answer when the PMS API fails

# Hotel Sense 0.6.2
- Exely API: a failing room list (Exely answered HTTP 500) no longer blocks the setup - sign-in is what the check requires. Without the list, rooms are matched by their Exely `roomId` (room mapping, e.g. `4503599627373585 = Room 07`); unmatched ones show the `roomId` in the last event. The list is retried at most once an hour
- The room list is requested with Exely's default page size (no `maxPageSize`)
- Action `exely_api_probe`: one request per Exely endpoint (sign-in, room list with / without `maxPageSize`, optional reservation), returns the answer structure or the error with Exely's `request_id`
- Error texts carry Exely's `request_id` header when present

# Hotel Sense 0.6.1
- Exely API check: the reason of a failure (HTTP status and Exely's error text, or the network error) is shown in the form ("Last error") and logged; it used to be swallowed. A non-JSON answer is reported instead of failing the form
- Settings show what is already set up and whether it works: the main menu lists the state of the Omada webhook, Exely webhook, Exely API and history database; the Exely API page shows "Now: set up (client ID abcd…, property …); last check …" like the database page

# Hotel Sense 0.6.0
- History database (Configure → History database): MariaDB add-on (or any MySQL / MariaDB) with host, port, user, password, database and retention (months, default 12); checked and tables created on save; password redacted
- Recorded: Omada webhook client events (online / offline / roaming), presence sessions per room, room state changes, status changes with source and booking, Exely events
- Batched, non-blocking writes; rows wait in memory while the database is down; nightly purge of rows older than the retention
- Only MACs, SSIDs, rooms and booking numbers are stored: no client names or IP addresses

# Hotel Sense 0.5.0
- Exely API (Configure → Exely API): webhook events that name only the booking get their room from the Exely Connect API (reservation → room stay → room list → Area); credentials are checked on save and redacted in diagnostics
- Few requests: reservation cached for the stay, room list on disk (daily refresh), token per 14 min, own limit 1 request/s and 30/h, `retry-after` honoured, 2 retries at most; repeated deliveries (same `eventId`) ignored
- Bookings with several rooms: only the room stays whose status matches the event
- Webhook payloads with several events are handled event by event; new result `api_error`
- Diagnostics: API counters, last error, cached room list and the shape (no values) of recent reservation responses

# Hotel Sense 0.4.0
- Sleeping devices: optional separate timeout for devices last seen in Wi-Fi power save (off by default); `power_save` in the room device lists, count in diagnostics
- Omada webhook (Configure → Omada webhook): authenticated controller messages trigger an immediate, debounced poll; recent messages in diagnostics; secret can be regenerated
- Actions: `reconnect_client`, `block_client`, `unblock_client` (by MAC); `ap_ssids` and `set_ap_ssid` (SSID on/off per access point or for all access points of a room)
- The Omada connection uses the library's public connection and site client (also for the SSID endpoint)

# Hotel Sense 0.3.1
- Options of the removed ha-omada features are deleted automatically on startup
- Reconfigure: the reload is left to the entry's update listener (connection settings changed), as Home Assistant requires from 2026.12 (it logged a deprecation warning)

# Hotel Sense 0.3.0
- Omada connection on `tplink-omada-client` (the library of Home Assistant's built-in TP-Link Omada integration, MIT); the forked ha-omada code is gone. Full Omada 6 support (client list via OpenAPI v2 from 6.2)
- Kept with the same IDs: rooms, access point devices (Areas stay), AP *Uptime* / *Clients*, controller *Clients*, misplaced devices, Exely
- New: AP *Status* (connectivity) replaces the AP device trackers
- Removed (cleaned up on the first start): per-client trackers / sensors / switches, firmware update entities, other ha-omada sensors and buttons; their options are dropped
- Reconfigure keeps the Exely webhook address and key (it used to regenerate them)
- Diagnostics show the Omada connection (version, APs, clients)
- MIT license; HACS license check on again

# Hotel Sense 0.2.1
- Room status select shows who set the status: `source` = manual / exely / restored (after a restart), `changed_at`, and `set_by` for a restored status
- Controller outage: a failing re-login no longer aborts the poll; rooms are flagged `data_stale`
- Room device lists (`devices`, `types`) are no longer written to the recorder database
- Access point devices are always on (the "track devices" option is gone: rooms need them)
- Exely settings: `Authentication: API-KEY`; the webhook address is always shown as https
- Brand icon; HACS validation passes
- Exely: cancelled check-in / check-out undo the status (previously a "CheckInCancelled" event would have been read as a check-in)
- Access points always get Uptime and Clients (total) sensors; CPU/memory and per-band/guest/user counts stay behind their options
- Removed "Start WLAN Optimization", "WLAN Optimization Running", "Reconnect All Clients" and per-client "Reconnect" (not needed; reconnect failed on controller v6); existing entities are cleaned up on load
- Room device lists show access point, SSID, signal, last seen and whether the device is still connected
- Device type per device (list → Omada → hostname → private MAC → unknown), `types` counts, shown on the dashboard; `device_type` column in the device list
- Settings texts fully follow the interface language (the "new key generated" notice no longer depends on the server language); English dashboard `dashboards/hotel_sense_rooms_en.yaml`
- Exely webhook: regenerate a compromised API key and/or webhook address from the settings
- Exely PMS webhook: check-in / check-out sets the room status (API-KEY header, room matching by number or mapping, `sensor.hotel_sense_exely_last_event`, recent payloads in diagnostics)
- Room status is now Exely check-in / check-out: `checked_in` / `checked_out` (saved 0.2.0 statuses migrate: sold -> checked_in, vacant/cleaning -> checked_out); cleaning states removed
- Device list: separate `room` column (where equipment is installed); `note` for department/role
- `sensor.hotel_sense_misplaced_devices`: fixed devices seen outside their `room`, warning on the dashboard

# Hotel Sense 0.2.0 (Stage A: room presence)
- Room = HA Area, location = Area of the client's access point
- `hotel_sense.ap_area_report` (APs without Area / mismatching a MAC table / not found) and `hotel_sense.assign_ap_areas` (MAC -> room table, matched by MAC)
- Editable list of fixed and employee devices in `.storage` (Options flow add/edit/delete, CSV import/export, MAC normalisation; `import_devices` / `export_devices` actions)
- Presence engine: Wi-Fi only, disconnect timeout (5 min), roaming debounce (30 s), optional minimum RSSI, outage keeps the last picture
- Per-room entities: guest/employee presence, device counts, manual status select (Свободен/Продан/Уборка), room state and violation; `hotel_sense_room_state_changed` event
- Lovelace rooms dashboard (built-in cards, red violations) and its generator
- Only the controller and access points become HA devices: connected clients (phones, TVs, ACs ...) no longer get devices/entities by default (option "create devices for connected clients", off), switches/gateways never; such devices left over from 0.1.0 are removed on load. Clients are always polled for presence
- Russian translation
- Restored CI workflows lost in the web upload; hassfest runs on every push

# Hotel Sense 0.1.0 (Phase 0)
- Renamed to Hotel Sense (domain `hotel_sense`, Powered by Omada). Fork of zachcheatham/ha-omada 0.9.0 (dev be6985d); Omada API/core unchanged (only additive `Controller.site_id`)
- Unique IDs namespaced: `ap:<site>:<mac>[:<key>]`, `client:<site>:<mac>[:<key>]`, `update:<site>:<mac>` (fresh domain: no migration from upstream)
- AP and client devices/entities separated (identifiers, in-memory buckets)
- Fixed deprecated HA APIs: ScannerEntity/SourceType import, DeviceInfo `default_name`
- Added pytest suite (HA 2026.9.4)

# Changelog

## 0.9.0
- Added reconnect all clients button
- Added POE Statistics (Thanks @bmadzinski)
- Added option to reconfigure integration (user, password, controller url)
- Fixed errors when reloading the integration
- Fixed issue when catching unknown site errors during setup.

## 0.8.0
- Added device reboot button (Thanks @RvRijsselt)
- Fixed OptionsFlow incompatibility with Home Assistant 2025.12

## 0.7.0
- Added wired client support (Thanks @jonathanhoskin)
- Added latest WiFi standards (Thanks @jonathanhoskin)

## 0.6.1
- Fixes issue related to timeouts in newer versions of HA
- Fixes issue related to missing IP addresses from the Omada Controlller
- Fixes regression causing entities to remain after the device was removed from the Omada Controller

## 0.5.0
- Add support for Omada Controller 5.13+
- Add sensor and trigger for WLAN optimization
- Fix device upgrades

## 0.4.3
- Fixes bug causing the configuration flow to break.

## 0.4.2
- Fix errors related to tx and rx rates on unsupported devices
- Fix an error when re-configuring the SSID filter after an SSID was removed from Omada.

## 0.4.1
- Fix issue related to pulling radio state and wifi mode of wired devices.
- Increase the API request timeout to 30 seconds.

## 0.4.0
- Added client bandwidth, activity, and uptime sensors.
- Added client WiFi blocking switches.
- Added HA devices to associate client entities together.
- Added device bandwidth, uptime, clients, CPU, and memory sensors.
- Added AP band utilization sensors.
- Added AP radio switches
- Added new integration configuration options for controlling which entity types are loaded.
- Added new integration configuration to add an additional timeout to device trackers before they are marked Away.
- Entities will now move to unavailable state when the controller becomes unreachable.
- Client device tracker attributes now include the mac address, host name, and connected AP name.
- Client device tracker AP mac addresses are now formatted as 00:00:00:00:00:00
- Removed site name prefix from access point devices.
- Remove use of deprecated HA functions.


## 0.3.0
- Added additional AP and client information and statistics (@ping-localhost)

## 0.2.1
- Fixed a permissions error by using a different endpoint for determing the site id (@sogood007)
- Allow Home Assistant to properly retry the integration's setup if Omada is unavailable when Home Assistant starts.

## 0.2.0

- Add support for the API changes introduced in version 5.0.0 of the Omada Controller. This has not been fully tested as I do not have a production v5 controller to test on. It is fully working on my device-less test install of 5.0.15

## 0.1.2

- Restore compatibility with devices running 4.4.6 as hardware controllers are still stuck on this version.

## 0.1.1
- Re-attempt logins after connectivity failures. This should fix lost connections after controller restarts.
- Add support for controller 4.4.8
- Remove support for controllers < 4.4.8 (Pre-Log4J Patches)
- Enhance error messaging during setup

## 0.1.0
- Add device state and tracking (PR #1)

## 0.0.3
- Fix errors when client disappears from known client list.

## 0.0.2
- Fix restoring filtered devices.
- Listen for HA configuration changes

## 0.0.1
- Initial Release