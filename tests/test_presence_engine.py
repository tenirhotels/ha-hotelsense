"""Presence engine: timeouts, roaming debounce, RSSI, classification, rules."""
from __future__ import annotations

import pytest

from custom_components.hotel_sense.device_list import CATEGORY_EMPLOYEE, CATEGORY_FIXED
from custom_components.hotel_sense.presence import (
    STATE_CHECKED_IN, STATE_EMPTY, STATE_STAFF_VISIT, STATE_VIOLATION, STATUS_CHECKED_IN,
    STATUS_CHECKED_OUT,
    AreaPresence, Observation, PresenceEngine, evaluate_room,
)

AP1, AP2, AP_NO_AREA = "AA-AA-AA-00-00-01", "AA-AA-AA-00-00-02", "AA-AA-AA-00-00-99"
AREAS = {AP1: "room_01", AP2: "room_02", AP_NO_AREA: None}
PHONE = "00-11-22-33-44-01"


def where(engine: PresenceEngine, mac: str = PHONE) -> str | None:
    track = engine.tracks.get(mac)
    return track.area_id if track else None


def test_first_sighting_is_located_immediately():
    e = PresenceEngine()
    e.update(0, [Observation(PHONE, AP1, -50)], AREAS)
    assert where(e) == "room_01"


def test_disconnect_timeout_keeps_device_for_5_minutes():
    e = PresenceEngine(timeout=300)
    e.update(0, [Observation(PHONE, AP1)], AREAS)
    e.update(299, [], AREAS)
    assert where(e) == "room_01"
    e.update(300, [], AREAS)
    assert where(e) is None


def test_reappearing_device_resets_timeout():
    e = PresenceEngine(timeout=300)
    e.update(0, [Observation(PHONE, AP1)], AREAS)
    e.update(250, [Observation(PHONE, AP1)], AREAS)
    e.update(500, [], AREAS)
    assert where(e) == "room_01"


def test_roaming_debounce_30_seconds():
    e = PresenceEngine(debounce=30)
    e.update(0, [Observation(PHONE, AP1)], AREAS)
    e.update(10, [Observation(PHONE, AP2)], AREAS)
    assert where(e) == "room_01"          # just roamed: not moved yet
    e.update(39, [Observation(PHONE, AP2)], AREAS)
    assert where(e) == "room_01"
    e.update(40, [Observation(PHONE, AP2)], AREAS)
    assert where(e) == "room_02"          # stayed 30 s on the new AP


def test_flapping_between_aps_does_not_move_device():
    e = PresenceEngine(debounce=30)
    e.update(0, [Observation(PHONE, AP1)], AREAS)
    for t in range(10, 200, 20):
        ap = AP2 if (t // 20) % 2 == 0 else AP1
        e.update(t, [Observation(PHONE, ap)], AREAS)
        assert where(e) == "room_01", t


def test_zero_debounce_moves_immediately():
    e = PresenceEngine(debounce=0)
    e.update(0, [Observation(PHONE, AP1)], AREAS)
    e.update(1, [Observation(PHONE, AP2)], AREAS)
    assert where(e) == "room_02"


def test_weak_rssi_never_moves_device():
    e = PresenceEngine(debounce=0, min_rssi=-75)
    e.update(0, [Observation(PHONE, AP1, -60)], AREAS)
    e.update(1, [Observation(PHONE, AP2, -85)], AREAS)   # neighbouring AP, weak
    assert where(e) == "room_01"
    e.update(2, [Observation(PHONE, AP2, -70)], AREAS)   # strong enough
    assert where(e) == "room_02"


def test_weak_rssi_device_is_unlocated_but_still_tracked():
    e = PresenceEngine(min_rssi=-75)
    e.update(0, [Observation(PHONE, AP1, -90)], AREAS)
    assert PHONE in e.tracks and where(e) is None
    e.update(5, [Observation(PHONE, AP1, -60)], AREAS)
    assert where(e) == "room_01"


def test_rssi_filter_off_by_default_and_missing_rssi_ignored():
    e = PresenceEngine(debounce=0)
    e.update(0, [Observation(PHONE, AP1, -95)], AREAS)
    assert where(e) == "room_01"
    e = PresenceEngine(debounce=0, min_rssi=-75)
    e.update(0, [Observation(PHONE, AP1, None)], AREAS)
    assert where(e) == "room_01"


def test_ap_without_area_gives_no_room():
    e = PresenceEngine()
    e.update(0, [Observation(PHONE, AP_NO_AREA)], AREAS)
    assert where(e) is None
    assert e.presence_by_area(lambda _: None) == {}


def test_presence_by_area_classifies():
    ac, maid, guest = "00-11-22-33-44-AC", "00-11-22-33-44-0E", "02-11-22-33-44-01"
    cats = {ac: CATEGORY_FIXED, maid: CATEGORY_EMPLOYEE}
    e = PresenceEngine()
    e.update(0, [Observation(m, AP1) for m in (ac, maid, guest, PHONE)], AREAS)
    p = e.presence_by_area(cats.get)["room_01"]
    assert p.fixed == {ac} and p.employee == {maid} and p.guest == {guest, PHONE}
    assert p.random_mac_count == 1


def _p(guest=0, employee=0, fixed=0) -> AreaPresence:
    p = AreaPresence()
    p.guest = {f"g{i}" for i in range(guest)}
    p.employee = {f"e{i}" for i in range(employee)}
    p.fixed = {f"f{i}" for i in range(fixed)}
    return p


@pytest.mark.parametrize(("status", "presence", "expected"), [
    # Checked out
    (STATUS_CHECKED_OUT, _p(guest=1), STATE_VIOLATION),
    (STATUS_CHECKED_OUT, _p(guest=1, employee=1), STATE_VIOLATION),
    (STATUS_CHECKED_OUT, _p(employee=1), STATE_STAFF_VISIT),
    (STATUS_CHECKED_OUT, _p(employee=1, fixed=2), STATE_STAFF_VISIT),
    (STATUS_CHECKED_OUT, _p(fixed=2), STATE_EMPTY),
    (STATUS_CHECKED_OUT, _p(), STATE_EMPTY),
    (None, _p(guest=1), STATE_VIOLATION),          # status not set yet: alert
    # Checked in
    (STATUS_CHECKED_IN, _p(guest=3), STATE_CHECKED_IN),
    (STATUS_CHECKED_IN, _p(employee=1), STATE_CHECKED_IN),
    (STATUS_CHECKED_IN, _p(), STATE_CHECKED_IN),
])
def test_violation_rules(status, presence, expected):
    assert evaluate_room(status, presence) == expected


# --------------------------------------------------------------------------- #
# Sleeping devices (Wi-Fi power save)
# --------------------------------------------------------------------------- #
def test_sleep_timeout_applies_to_devices_last_seen_in_power_save():
    e = PresenceEngine(timeout=120, sleep_timeout=600)
    awake, asleep = "00-11-22-33-44-02", "00-11-22-33-44-03"
    e.update(0, [Observation(awake, AP1, power_save=False),
                 Observation(asleep, AP1, power_save=True)], AREAS)
    e.update(120, [], AREAS)
    assert where(e, awake) is None          # normal timeout
    assert where(e, asleep) == "room_01"    # still asleep in the room
    e.update(600, [], AREAS)
    assert where(e, asleep) is None


def test_last_observation_decides_power_save():
    e = PresenceEngine(timeout=120, sleep_timeout=600)
    e.update(0, [Observation(PHONE, AP1, power_save=True)], AREAS)
    e.update(10, [Observation(PHONE, AP1, power_save=False)], AREAS)  # woke up, then left
    e.update(130, [], AREAS)
    assert where(e) is None


def test_sleep_timeout_off_by_default():
    e = PresenceEngine(timeout=120)
    e.update(0, [Observation(PHONE, AP1, power_save=True)], AREAS)
    e.update(120, [], AREAS)
    assert where(e) is None
