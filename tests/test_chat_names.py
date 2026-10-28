"""Room names are membership labels, never shared identity or authorization."""

import base64
import copy
import json
import secrets

import pytest
from message_key_fixtures import public_key
from test_admin_lifecycle import client, headers, make_link
from test_conversation_closure import group

__all__ = ["client"]


def join_named(client, token, name, *, person=None, resume=None, credential=None,
               authenticated=True):
    response = client.post("/api/v1/links/activate", json={
        "token": token,
        "public_key": person["user"]["public_key"] if person else (
            base64.b64encode(public_key()).decode()),
        "display_name": name,
        "resume_credential": resume or secrets.token_urlsafe(32),
        "session_credential": credential or secrets.token_urlsafe(32),
    }, headers=headers(person) if person and authenticated else {})
    assert response.status_code == 200
    return response.json()


def room_name(chat, public_id):
    return next(peer["display_name"] for peer in chat["participants"]
                if peer["public_id"] == public_id)


@pytest.mark.parametrize("create_link", [make_link, group])
def test_shared_identity_keeps_a_different_name_in_each_room(client, create_link):
    _, first_token = create_link()
    first = join_named(client, first_token, "Alice")
    _, second_token = create_link()
    second = join_named(client, second_token, "Room Two", person=first)
    _, third_token = create_link()
    third = join_named(client, third_token, "Room Three", person=second)
    public_id = first["user"]["public_id"]
    assert second["user"]["public_id"] == third["user"]["public_id"] == public_id
    assert second["user"]["public_key"] == first["user"]["public_key"]
    assert second["user"]["display_name"] == "Room Two"
    assert third["user"]["display_name"] == "Room Three"
    expected = {first["chat"]["id"]: "Alice", second["chat"]["id"]: "Room Two",
                third["chat"]["id"]: "Room Three"}
    for login in (first, second, third):
        profile = client.get("/api/v1/me", headers=headers(login))
        assert profile.status_code == 200
        assert profile.json()["user"]["display_name"] == "Alice"
        assert {chat["id"]: room_name(chat, public_id)
                for chat in profile.json()["chats"]} == expected
        for chat_id, name in expected.items():
            response = client.get(f"/api/v1/chats/{chat_id}", headers=headers(login))
            assert response.status_code == 200
            assert room_name(response.json(), public_id) == name
    peer = join_named(client, second_token, "Peer")
    assert room_name(peer["chat"], public_id) == "Room Two"


@pytest.mark.parametrize("name,expected", [(None, None), ("", None), ("   ", None),
                                          ("  Room name  ", "Room name")])
def test_optional_room_name_is_normalized_without_inheriting_the_account_label(
    client, name, expected,
):
    _, first_token = make_link()
    first = join_named(client, first_token, "Account label")
    _, second_token = make_link()
    second = join_named(client, second_token, name, person=first)
    assert second["user"]["display_name"] == expected
    assert room_name(second["chat"], first["user"]["public_id"]) == expected
    old_room = client.get(f"/api/v1/chats/{first['chat']['id']}", headers=headers(second))
    assert room_name(old_room.json(), first["user"]["public_id"]) == "Account label"


@pytest.mark.parametrize("authenticated", [True, False])
def test_consumed_invite_retries_and_reconnects_keep_the_original_room_name(
    client, authenticated,
):
    _, token = make_link()
    resume, credential = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    first = join_named(client, token, "Original room name", resume=resume, credential=credential)
    join_named(client, token, "Peer")
    before = client.get("/admin/api/stats").json()
    retry = join_named(client, token, "Must not rename", person=first, resume=resume,
                       credential=credential, authenticated=authenticated)
    assert retry["user"]["public_id"] == first["user"]["public_id"]
    assert retry["user"]["display_name"] == "Original room name"
    assert room_name(retry["chat"], first["user"]["public_id"]) == "Original room name"
    assert client.get("/admin/api/stats").json() == before


def test_configuration_round_trip_preserves_room_names_for_the_same_identity(client):
    _, first_token = make_link()
    first = join_named(client, first_token, "First room")
    _, second_token = make_link()
    join_named(client, second_token, "Second room", person=first)
    exported = client.get("/admin/api/export").json()
    assert exported["version"] == 2
    selected_links = [copy.deepcopy(next(link for link in exported["links"]
                                        if link["token"] == token))
                      for token in (first_token, second_token)]
    selected_chats = [exported["chats"][link["chat_index"]] for link in selected_links]
    for index, link in enumerate(selected_links):
        link["chat_index"] = index
        link["token"] = secrets.token_urlsafe(32)
    bundle = {"version": 2, "users": [user for user in exported["users"]
                                     if user["public_id"] == first["user"]["public_id"]],
              "chats": selected_chats, "links": selected_links}
    response = client.post("/admin/api/import", files={
        "bundle": ("room-names.json", json.dumps(bundle), "application/json"),
    })
    assert response.status_code == 200
    for link, name in zip(selected_links, ("First room", "Second room"), strict=True):
        restored = join_named(client, link["token"], "Ignored on reconnect", person=first)
        assert restored["user"]["display_name"] == name
        assert room_name(restored["chat"], first["user"]["public_id"]) == name


@pytest.mark.parametrize("invalid", ["missing", 123, {}, [], "x" * 65])
def test_invalid_import_names_are_rejected_before_replace_changes_existing_data(client, invalid):
    _, token = make_link()
    first = join_named(client, token, "Retained name")
    bundle = client.get("/admin/api/export").json()
    link = next(link for link in bundle["links"] if link["token"] == token)
    member = bundle["chats"][link["chat_index"]]["members"][0]
    if invalid == "missing":
        del member["display_name"]
    else:
        member["display_name"] = invalid
    before = client.get("/admin/api/stats").json()
    response = client.post("/admin/api/import?replace=true", files={
        "bundle": ("invalid-name.json", json.dumps(bundle), "application/json"),
    })
    assert response.status_code == 400
    assert client.get("/admin/api/stats").json() == before
    profile = client.get("/api/v1/me", headers=headers(first))
    assert profile.status_code == 200
    assert room_name(profile.json()["chats"][0], first["user"]["public_id"]) == "Retained name"


def test_old_bundle_format_is_rejected_before_replace_changes_existing_data(client):
    _, token = make_link()
    first = join_named(client, token, "Retained name")
    bundle = client.get("/admin/api/export").json()
    bundle["version"] = 1
    before = client.get("/admin/api/stats").json()
    response = client.post("/admin/api/import?replace=true", files={
        "bundle": ("unsupported.json", json.dumps(bundle), "application/json"),
    })
    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported bundle version"
    assert client.get("/admin/api/stats").json() == before
    assert client.get("/api/v1/me", headers=headers(first)).status_code == 200
