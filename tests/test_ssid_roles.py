"""Wi-Fi network roles (hints)."""
from __future__ import annotations

import pytest

from custom_components.hotel_sense.ssid_roles import NetworkRole, SsidRoles

TEXT = """
# hotel networks
GUEST{n}      = guest_room
Hotel-Service = персонал
iot           = equipment
lab-*         = fixed
"""


def test_roles():
    roles = SsidRoles.parse(TEXT)
    assert roles.role_of("guest07") == NetworkRole("guest_room", "7")
    assert roles.role_of("GUEST10") == NetworkRole("guest_room", "10")
    assert roles.role_of("hotel-service") == NetworkRole("staff")
    assert roles.role_of("IOT") == NetworkRole("equipment")
    assert roles.role_of("lab-2") == NetworkRole("equipment")
    assert roles.role_of("GUESTX") is None and roles.role_of("other") is None
    assert roles.role_of(None) is None
    assert roles.roles_of(["iot", "GUEST1", "nope"]) == {"equipment", "guest_room"}
    assert bool(roles) and not SsidRoles.parse("")


@pytest.mark.parametrize("bad", ["iot", "= staff", "iot = boss"])
def test_bad_lines_are_named(bad):
    with pytest.raises(ValueError, match="line 2"):
        SsidRoles.parse(f"iot = equipment\n{bad}")
