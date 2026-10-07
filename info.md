# Hotel Sense

Presence and occupancy for hotel rooms on Home Assistant, from TP-Link Omada
Wi-Fi: which room has guest, staff and fixed devices, room status (check-in /
check-out, Exely PMS webhook) and violations (devices in a room nobody is
checked into).

* Room = Home Assistant Area, location = the Area of the client's access point.
* Known-device list: fixed equipment and staff devices (CSV import / export).
* Exely PMS webhook sets the room status; manual select as fallback.
* Dashboards in `dashboards/` (English and Russian).

Requires an Omada SDN controller 5.1 or newer (tested with 6.3). Uses
[`tplink-omada-client`](https://github.com/MarkGodwin/tplink-omada-api), the
library of Home Assistant's built-in TP-Link Omada integration.
