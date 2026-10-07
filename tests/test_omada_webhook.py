"""Omada controller webhook: authenticated messages trigger an immediate poll."""
from __future__ import annotations

from homeassistant.data_entry_flow import FlowResultType

from custom_components.hotel_sense.const import (
    CONF_OMADA_WEBHOOK_ID, CONF_OMADA_WEBHOOK_SECRET, DOMAIN,
)
from custom_components.hotel_sense.diagnostics import async_get_config_entry_diagnostics

from .fakes import client_raw
from .test_stage_a import _hotel, _options_menu, _state

# Shaped like Omada's webhook message (site, controller, text lines, secret).
MESSAGE = {"Site": "Test Site", "description": "This is a webhook message from Omada Controller",
           "Controller": "Omada OC200", "timestamp": 1760000000000,
           "text": ["[client:02-00-00-00-00-09] connected to [ap:WF06]"]}


async def _post(client, entry, payload, *, secret=None, header=False):
    url = f"/api/webhook/{entry.data[CONF_OMADA_WEBHOOK_ID]}"
    secret = entry.data[CONF_OMADA_WEBHOOK_SECRET] if secret is None else secret
    if header:
        return await client.post(url, json=payload, headers={"Shard-Secret": secret})
    return await client.post(url, json=payload | {"shardSecret": secret})


async def test_secrets_generated_and_kept(hass, make_entry, patch_api):
    entry = make_entry()
    await _hotel(hass, entry)
    ids = (entry.data[CONF_OMADA_WEBHOOK_ID], entry.data[CONF_OMADA_WEBHOOK_SECRET])
    assert len(ids[0]) >= 32 and len(ids[1]) >= 24
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (entry.data[CONF_OMADA_WEBHOOK_ID], entry.data[CONF_OMADA_WEBHOOK_SECRET]) == ids


async def test_message_triggers_immediate_poll(hass, make_entry, patch_api, hass_client_no_auth):
    entry = make_entry()
    await _hotel(hass, entry)
    client = await hass_client_no_auth()
    # A new guest phone appears in Omada; without the webhook HA would see it
    # only at the next scan interval.
    patch_api.responses["/clients"]["data"].append(client_raw("02-00-00-00-00-09", "New-Phone"))
    polls = patch_api.poll_calls
    resp = await _post(client, entry, MESSAGE)
    await hass.async_block_till_done()
    assert resp.status == 200
    assert patch_api.poll_calls == polls + 1
    assert _state(hass, "sensor.room_06_guest_devices") == "3"


async def test_secret_in_header_also_accepted(hass, make_entry, patch_api, hass_client_no_auth):
    entry = make_entry()
    await _hotel(hass, entry)
    resp = await _post(await hass_client_no_auth(), entry, MESSAGE, header=True)
    assert resp.status == 200


async def test_wrong_or_missing_secret_rejected(hass, make_entry, patch_api, hass_client_no_auth):
    entry = make_entry()
    await _hotel(hass, entry)
    client = await hass_client_no_auth()
    polls = patch_api.poll_calls
    assert (await _post(client, entry, MESSAGE, secret="wrong")).status == 401
    url = f"/api/webhook/{entry.data[CONF_OMADA_WEBHOOK_ID]}"
    assert (await client.post(url, json=MESSAGE)).status == 401
    assert (await client.post(url, data="not json")).status == 401
    await hass.async_block_till_done()
    assert patch_api.poll_calls == polls
    controller = hass.data[DOMAIN][entry.entry_id]
    assert controller.omada_webhook.rejected == 3 and controller.omada_webhook.received == 0


async def test_burst_of_messages_is_debounced(hass, make_entry, patch_api, hass_client_no_auth):
    entry = make_entry()
    await _hotel(hass, entry)
    client = await hass_client_no_auth()
    polls = patch_api.poll_calls
    for _ in range(5):
        assert (await _post(client, entry, MESSAGE)).status == 200
    await hass.async_block_till_done()
    assert patch_api.poll_calls - polls <= 2  # immediate + at most one after the cooldown


async def test_diagnostics_keep_messages_without_secret(hass, make_entry, patch_api,
                                                        hass_client_no_auth):
    entry = make_entry()
    await _hotel(hass, entry)
    await _post(await hass_client_no_auth(), entry, MESSAGE)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    hook = diag["omada_webhook"]
    assert hook["received"] == 1 and hook["recent"][0]["payload"] == MESSAGE
    assert entry.data[CONF_OMADA_WEBHOOK_SECRET] not in str(diag)
    assert diag["entry"][CONF_OMADA_WEBHOOK_SECRET] == "**REDACTED**"
    assert diag["entry"][CONF_OMADA_WEBHOOK_ID] == "**REDACTED**"


async def test_options_show_url_and_secret_and_rotate(hass, make_entry, patch_api,
                                                       hass_client_no_auth):
    await hass.config.async_update(internal_url="http://192.0.2.10:8123")
    entry = make_entry()
    controller = await _hotel(hass, entry)
    result = await _options_menu(hass, entry, "omada_webhook")
    assert result["step_id"] == "omada_webhook"
    ph = result["description_placeholders"]
    assert ph["url"] == f"http://192.0.2.10:8123/api/webhook/{entry.data[CONF_OMADA_WEBHOOK_ID]}"
    assert ph["secret"] == entry.data[CONF_OMADA_WEBHOOK_SECRET]

    old = entry.data[CONF_OMADA_WEBHOOK_SECRET]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"omada_new_secret": True})
    assert result["step_id"] == "omada_webhook_rotated"
    new = entry.data[CONF_OMADA_WEBHOOK_SECRET]
    assert new != old and result["description_placeholders"]["secret"] == new
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id] is controller  # no reload for a new secret

    client = await hass_client_no_auth()
    assert (await _post(client, entry, MESSAGE, secret=old)).status == 401
    assert (await _post(client, entry, MESSAGE)).status == 200
