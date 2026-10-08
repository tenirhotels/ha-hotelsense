"""Device identity suggestions: pure logic."""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.hotel_sense.device_suggest import (
    MIN_SCORE, Profile, Session, compare, normalise_name, suggest,
)

D0 = datetime(2026, 1, 5)  # a Monday
NEW_MAC, OTHER_MAC = "02-00-00-00-00-81", "02-00-00-00-00-82"


def _workday(day: int, zones=("admin_house", "room_03", "room_05"), start=8) -> list[Session]:
    """A housekeeping day: an hour in each zone from ``start`` (UTC)."""
    t = D0 + timedelta(days=day, hours=start)
    return [Session(z, t + timedelta(hours=i), t + timedelta(hours=i + 1))
            for i, z in enumerate(zones)]


def _staff() -> Profile:
    sessions = [s for d in range(5) for s in _workday(d)]
    return Profile(names={"xiaomi-14"}, label="Xiaomi-14", models={"xiaomi 14"}, ssids={"Staff"},
                   sessions=sessions)


def test_new_mac_of_a_staff_phone_scores_on_every_check():
    new = Profile(names={"xiaomi-14"}, label="Xiaomi-14", models={"xiaomi 14"}, ssids={"Staff"},
                  sessions=_workday(6))  # the old MAC went quiet on day 4
    score, checks = compare(new, _staff())
    assert score == 100
    assert {c["check"]: c["ok"] for c in checks} == {
        "name": True, "model": True, "handoff": True, "zones": True, "hours": True,
        "ssid": True}
    assert next(c for c in checks if c["check"] == "handoff")["detail"] == 45.0  # hours


def test_on_the_wifi_at_the_same_time_is_never_the_same_device():
    new = Profile(names={"xiaomi-14"}, ssids={"Staff"}, sessions=_workday(4))
    assert compare(new, _staff()) is None


def test_too_little_data_is_reported_not_guessed():
    t = D0 + timedelta(days=6, hours=8)
    new = Profile(names={"xiaomi-14"}, sessions=[Session("lobby", t, t + timedelta(minutes=20))])
    score, checks = compare(new, _staff())
    by = {c["check"]: c["ok"] for c in checks}
    assert by == {"name": True, "model": None, "handoff": True, "zones": None, "hours": None,
                  "ssid": None}
    assert score == 50 < MIN_SCORE  # a default name alone is not enough
    new.models = {"xiaomi 14"}
    assert compare(new, _staff())[0] == 65


def test_another_model_counts_against():
    new = Profile(names={"xiaomi-14"}, models={"galaxy a16"}, ssids={"Staff"},
                  sessions=_workday(6))
    score, checks = compare(new, _staff())
    assert {c["check"]: c["ok"] for c in checks}["model"] is False
    assert score == 70


def test_a_guest_with_the_same_phone_model_is_not_suggested():
    # Same default name, but in one guest room at night, on the guest Wi-Fi:
    # name and handoff hold, zones, hours and network do not.
    t = D0 + timedelta(days=6, hours=20)
    guest = Profile(names={"xiaomi-14"}, ssids={"Guest"},
                    sessions=[Session("room_07", t, t + timedelta(hours=10))])
    score, checks = compare(guest, _staff())
    assert {c["check"]: c["ok"] for c in checks} == {
        "name": True, "model": None, "handoff": True, "zones": False, "hours": False,
        "ssid": False}
    assert score == 15 < MIN_SCORE
    assert suggest({NEW_MAC: guest}, {"7": ({"name": "Maid"}, _staff())}) == []


def test_suggest_picks_the_best_device_and_skips_ignored_pairs():
    new = Profile(names={"xiaomi-14"}, label="Xiaomi-14", models={"xiaomi 14"}, ssids={"Staff"},
                  sessions=_workday(6))
    other = Profile(names={"galaxy"}, ssids={"Staff"},
                    sessions=[s for d in range(5) for s in _workday(d, ("lobby",), start=20)])
    devices = {"7": ({"name": "Maid phone", "category": "employee"}, _staff()),
               "3": ({"name": "Night porter", "category": "employee"}, other)}
    found = suggest({NEW_MAC: new}, devices)
    assert [(s["mac"], s["identity"], s["score"]) for s in found] == [(NEW_MAC, "7", 100)]
    assert found[0]["name"] == "Xiaomi-14" and found[0]["identity_name"] == "Maid phone"
    assert suggest({NEW_MAC: new}, devices, ignored={(NEW_MAC, "7")}) == []
    # Nothing in common: no suggestion.
    stranger = Profile(names={"pixel"}, ssids={"Guest"}, sessions=_workday(6, ("room_09",), 22))
    assert suggest({OTHER_MAC: stranger}, devices) == []


def test_names():
    assert normalise_name(" Xiaomi-14 ") == "xiaomi-14"
    assert normalise_name("aa:bb:cc:dd:ee:ff") is None
    assert normalise_name("AA-BB-CC-DD-EE-FF") is None
    assert normalise_name("") is None and normalise_name(None) is None
