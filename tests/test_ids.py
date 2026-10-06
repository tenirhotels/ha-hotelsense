"""Unique-ID namespaces (Master ТЗ 6.2)."""
import pytest

from custom_components.hotel_sense.ids import (
    NS_AP, NS_CLIENT, NS_UPDATE, make_unique_id,
    normalize_mac, parse_unique_id,
)

MAC = "AA-BB-CC-DD-EE-FF"


def test_spec_formats():
    assert make_unique_id(NS_AP, "s1", MAC) == f"ap:s1:{MAC}"
    assert make_unique_id(NS_CLIENT, "s1", MAC) == f"client:s1:{MAC}"
    assert make_unique_id(NS_UPDATE, "s1", MAC) == f"update:s1:{MAC}"
    assert make_unique_id(NS_CLIENT, "s1", MAC, "downloaded") == f"client:s1:{MAC}:downloaded"


def test_same_mac_never_collides_across_namespaces():
    ids = {
        make_unique_id(ns, "s1", MAC, key)
        for ns in (NS_AP, NS_CLIENT)
        for key in (None, "downloaded", "rx")
    }
    assert len(ids) == 6  # 2 namespaces x 3 keys, all distinct


def test_same_mac_on_different_sites_differs():
    assert make_unique_id(NS_AP, "s1", MAC) != make_unique_id(NS_AP, "s2", MAC)


@pytest.mark.parametrize("mac", ["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", " aa-bb-cc-dd-ee-ff "])
def test_mac_normalisation(mac):
    assert normalize_mac(mac) == MAC
    assert make_unique_id(NS_AP, "s", mac) == f"ap:s:{MAC}"


@pytest.mark.parametrize("ns,key", [
    (NS_AP, None), (NS_CLIENT, None), (NS_UPDATE, None),
    (NS_AP, "downloaded"), (NS_CLIENT, "rx"),
    (NS_AP, "ssid_Guest WiFi"), (NS_AP, "ssid_Кафе-5G"),  # free-form SSID keys
])
def test_roundtrip(ns, key):
    parsed = parse_unique_id(make_unique_id(ns, "site-1", MAC, key))
    assert (parsed.namespace, parsed.site_id, parsed.mac, parsed.key) == (
        ns, "site-1", MAC, key)


def test_site_id_may_contain_colons_and_spaces():
    parsed = parse_unique_id(make_unique_id(NS_CLIENT, "Site: One", MAC, "rx"))
    assert (parsed.site_id, parsed.mac, parsed.key) == ("Site: One", MAC, "rx")


def test_non_namespaced_ids_are_rejected():
    assert parse_unique_id(MAC) is None
    assert parse_unique_id(f"downloaded-{MAC}") is None
    assert parse_unique_id("ai_optimization-64f1a2b3c4d5e6f7a8b9c0d1") is None
    assert parse_unique_id("") is None


def test_unknown_namespace_rejected():
    with pytest.raises(ValueError):
        make_unique_id("guest", "s1", MAC)
    with pytest.raises(ValueError):
        make_unique_id(NS_AP, "", MAC)
