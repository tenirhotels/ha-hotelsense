# Hotel Sense

**Powered by Omada.** Presence and occupancy intelligence for Tenir Hotel on Home Assistant. Domain: `hotel_sense`.

Hotel Sense talks to the TP-Link Omada SDN controller (5.1 or newer; tested
with 6.3) through [`tplink-omada-client`](https://github.com/MarkGodwin/tplink-omada-api),
the library of Home Assistant's built-in TP-Link Omada integration. Library
updates follow Home Assistant's: `manifest.json` only sets a minimum version,
so the version Home Assistant itself ships is used.


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
presence and get no devices; switches and gateways are skipped. Each access
point has *Uptime*, *Clients* and *Status* (connected to the controller); the
controller has *Clients* (whole site). Poll interval: Configure → Omada polling.

Network management (client blocking, PoE, firmware updates …) is not part of
Hotel Sense: use the Omada controller or Home Assistant's built-in TP-Link
Omada integration alongside it.

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
`invalid` / `api_error`). The last 20 raw payloads are kept in memory and can be downloaded
via the integration's *Download diagnostics* — they contain guest data, mask it
before sharing. The manual status select stays as a fallback.

If the key or the address leaks, tick *Generate a new API key* and/or *Generate
a new webhook address* in the same settings page: the old one stops working at
once and the form shows the new value to enter in Exely.

### Exely API: the room of a booking

Exely webhook events name the booking (`BookingNumber`, `PropertyId`), not the
room. With Exely Connect API access, Hotel Sense looks the room up:
Configure → *Exely API (room of a booking)* → client ID, client secret (both
from the API connection in Exely, read access to reservations and rooms) and
the property ID. Saving checks them with one room-list request. The secret is
stored in Home Assistant only and redacted in diagnostics.

Requests are kept to a minimum, Exely is only called when a webhook needs a room:

* reservation by number: one request per booking, cached for the stay (a
  single-room booking costs one request for check-in and check-out; a changed
  booking is looked up again); repeated webhook deliveries (same `eventId`) are
  ignored;
* room list (`roomId` → name): stored on disk, refreshed at most once a day, or
  once an hour when an unknown room appears;
* one token per 14 minutes; at most 1 request per second and 30 per hour;
  `429 retry-after` is honoured; at most 2 retries.

Exely room names are matched to Areas like webhook room names (the room
mapping applies). For a booking with several rooms, only the room stays Exely
shows with the event's status change. If the API fails, the event shows
`api_error`. Only room stay IDs, statuses and dates are used; diagnostics show
the response *structure* without values, plus request counters.

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
  Devices on every SSID are placed.
* A device that disappears stays in the room for the disconnect timeout.
* Roaming to an AP of another room is accepted after the debounce.
* With a minimum RSSI set, weaker signals never move a device (neighbouring AP).
* When the controller is unreachable, the last picture is kept (no mass "empty").
* Random MACs are not merged automatically.

---

### Sleeping devices (Wi-Fi power save)

Omada reports whether a phone is in Wi-Fi power save (screen off). Configure →
Room presence → *Timeout for sleeping devices*: a device whose last report was
in power save is kept in its room this long instead of the disconnect timeout
(0 = off, the default). Each device in the room lists shows `power_save`;
diagnostics count the devices in power save. Observe the real data before
relying on it: a guest leaving with the phone in the pocket is "asleep" too.

### Omada webhook: instant updates

Configure → *Omada webhook* shows a URL and a *Shard Secret*. In the Omada
controller: Settings → Webhook → add them, and send the client events
(connected / disconnected / roaming) and alerts to it. Any authenticated
message makes Hotel Sense poll the controller at once (debounced), so rooms
update within seconds. The webhook only accepts local-network requests;
recent messages (without the secret) are in the diagnostics.

### Actions (Developer tools → Actions)

| Action | What it does |
|---|---|
| `hotel_sense.reconnect_client` | Disconnects a Wi-Fi device (`mac`) so it reconnects: is it really there? |
| `hotel_sense.block_client` / `unblock_client` | Blocks / unblocks a device (`mac`) in Omada |
| `hotel_sense.ap_ssids` | Which SSIDs are enabled on each access point (or on `access_point`) |
| `hotel_sense.set_ap_ssid` | Turns an SSID on/off on an access point: `access_point` = MAC, AP name or a room (all its APs), `ssid`, `enabled` |

Nothing calls them automatically yet; they are the building blocks for later
automations (e.g. room SSID off at check-out).

## History database (MariaDB)

Hotel Sense keeps a history for later analysis (and the future web admin) in
its own database, separate from the Home Assistant recorder.

1. Install the **MariaDB** add-on and add to its configuration:
   ```yaml
   databases:
     - hotel_sense
   logins:
     - username: hotel_sense
       password: <choose one>
   rights:
     - username: hotel_sense
       database: hotel_sense
   ```
2. Hotel Sense → Configure → *History database (MariaDB)*: host `core-mariadb`,
   port `3306`, user, password, database `hotel_sense`, retention (months,
   default 12). Saving checks the connection and creates the tables; the
   password is never shown again (empty keeps it) and is redacted in diagnostics.
   An empty user switches the history off.

| Table | What |
|---|---|
| `wifi_events` | Omada webhook client events: online / offline / roaming, client and AP MACs, SSID, connected time, traffic |
| `presence_sessions` | a device was in a room (Area) from … to … (guest / employee / fixed), written when it leaves |
| `room_states` | room state changes (empty / violation / staff_visit / checked_in) with device counts |
| `room_status` | check-in / check-out and who set it (manual / exely) with the booking number |
| `pms_events` | Exely webhook events: event, booking, result, rooms |

Rows are queued and written in batches every 15 s; while the database is down
they wait in memory (up to 20 000 rows) and are written when it is back - Home
Assistant and the rooms never wait for it. Open presence sessions are written
on unload and when Home Assistant stops. Rows older than the retention period
are deleted every night at 04:17. Times are UTC. Only MACs, SSIDs, rooms and
booking numbers are stored: no client names, IP addresses or guest data.
Diagnostics show the writer state (connected, queued, written, last error).

## Installation

HACS → ⋮ → Custom repositories → `https://github.com/tenirhotels/ha-hotelsense`
(Integration) → install **Hotel Sense** → restart Home Assistant → Settings →
Devices & services → Add integration → **Hotel Sense**: controller URL (e.g.
`https://192.168.0.4`), site name as shown in Omada, a controller user
(a read-only *Viewer* account is enough), and whether to verify the certificate.

## Upgrading from 0.2

0.3 replaces the ha-omada based controller code with `tplink-omada-client`.
Access point devices keep their identifiers, so their Areas (and therefore the
rooms) stay as they are; room entities, the access points' *Uptime* / *Clients*
and the controller's *Clients* keep their entity IDs and history. Removed on
the first start: per-client trackers, sensors and switches, access point
device trackers (replaced by *Status*), firmware update entities and the other
ha-omada sensors and buttons.

## License

MIT, see [LICENSE](LICENSE). Hotel Sense started as a fork of
[zachcheatham/ha-omada](https://github.com/zachcheatham/ha-omada); since 0.3 none
of that code remains (see [NOTICE.md](NOTICE.md)).
