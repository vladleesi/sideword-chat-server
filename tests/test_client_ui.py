"""Client/invite structure, local assets and unchanged admin sign-in."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import create_app
from app.templates import templates

ROOT = Path(__file__).resolve().parents[1]


class Structure(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "parents": self.stack.copy()}
        self.nodes.append(node)
        if tag not in {"input", "meta", "link", "br", "hr", "img"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def by_id(self, identifier):
        matches = [node for node in self.nodes if node["attrs"].get("id") == identifier]
        assert len(matches) == 1
        return matches[0]


def test_client_retains_controls_accessibility_and_separate_mobile_views():
    html = (ROOT / "app/templates/client.html").read_text()
    tree = Structure(html)
    ids = [node["attrs"]["id"] for node in tree.nodes if "id" in node["attrs"]]
    assert len(ids) == len(set(ids))
    for identifier in (
        "activation-form", "invite-token", "display-name", "join-password",
        "display-name-error", "activation-error", "reconnect-session", "setup-panel",
        "client-panel", "chat-list", "refresh-button", "participant-count",
        "conversation-kind", "conversation-title", "participant-keys", "message-list",
        "message-form", "message-input", "send-button", "pending-sends",
        "pending-send-list", "retry-pending-sends", "reset-button", "identity-label",
        "session-countdown", "connection-state", "status-message", "error-message",
    ):
        tree.by_id(identifier)
    grid = tree.by_id("client-panel")
    assert grid["attrs"]["data-mobile-view"] == "chats"
    pending = tree.by_id("pending-sends")
    assert pending["parents"][-1] is grid
    warning = tree.by_id("key-change-warning")
    assert warning["attrs"]["role"] == "alert"
    assert all(node["tag"] != "dialog" for node in warning["parents"])
    for identifier in ("device-settings", "conversation-details"):
        dialog = tree.by_id(identifier)
        assert dialog["tag"] == "dialog"
        tree.by_id(dialog["attrs"]["aria-labelledby"])
    assert tree.by_id("participant-keys")["parents"][2]["attrs"]["id"] == "conversation-details"
    assert tree.by_id("message-list")["attrs"]["role"] == "log"
    assert tree.by_id("message-list")["attrs"]["aria-live"] == "polite"
    assert tree.by_id("message-input")["attrs"]["maxlength"] == "16000"
    assert tree.by_id("display-name-error")["attrs"]["role"] == "alert"
    assert tree.by_id("error-message")["attrs"]["role"] == "alert"
    for identifier, name in (("device-settings-button", "Device settings"),
                             ("conversation-details-button", "Conversation details")):
        button = tree.by_id(identifier)
        assert button["attrs"]["aria-label"] == name
        icons = [node for node in tree.nodes if node["tag"] == "svg"
                 and node["parents"][-1] is button]
        assert len(icons) == 1
        assert icons[0]["attrs"]["aria-hidden"] == "true"
        assert icons[0]["attrs"]["focusable"] == "false"
    assert tree.by_id("back-to-chats")["attrs"]["aria-label"] == "Back to conversations"
    assert "Test client · unaudited" in html
    assert "hidden" in tree.by_id("setup-panel")["attrs"]
    assert "hidden" not in tree.by_id("loading-panel")["attrs"]
    assert tree.by_id("loading-panel")["attrs"]["role"] == "status"
    assert "First-use key pinning is not identity verification." in html
    assert html.index('/static/appearance.js') < html.index('/static/client.css')
    assert all(node["attrs"].get("src", "").startswith("/static/")
               for node in tree.nodes if node["tag"] == "script")
    assert not any("style" in node["attrs"] or any(key.startswith("on") for key in node["attrs"])
                   for node in tree.nodes)


def test_viewport_and_full_technical_value_styles():
    css = (ROOT / "app/static/client.css").read_text()
    assert "height: 100dvh" in css
    assert "grid-template-columns: 280px minmax(0, 1fr)" in css
    assert '[data-mobile-view="chats"] .conversation { display: none; }' in css
    assert '[data-mobile-view="conversation"] .chat-sidebar { display: none; }' in css
    assert "white-space: pre-wrap" in css
    assert "overflow-wrap: anywhere" in css
    assert "prefers-reduced-motion" in css
    assert 'data-theme="dark"' in css
    assert "text-overflow: ellipsis" not in css
    assert "gradient(" not in css


def test_client_fonts_are_local_and_allowed_by_its_csp():
    with TestClient(create_app(), base_url="https://testserver") as client:
        response = client.get("/client")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        policy = response.headers["content-security-policy"]
        for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                          "font-src 'self'", "base-uri 'none'", "frame-ancestors 'none'"):
            assert directive in policy
        assert "unsafe-inline" not in policy
        for name in ("IBMPlexSans-Regular.woff2", "IBMPlexSans-Medium.woff2",
                     "IBMPlexSans-SemiBold.woff2", "IBMPlexMono-Regular.woff2", "OFL.txt"):
            asset = client.get(f"/static/fonts/{name}")
            assert asset.status_code == 200
            assert asset.content == (ROOT / "docs/fonts" / name).read_bytes()
        assert client.get("/static/appearance.js").status_code == 200


def test_admin_login_retains_original_styles_fields_csrf_and_errors():
    with TestClient(create_app(), base_url="https://testserver") as client:
        response = client.get("/admin/login")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        tree = Structure(response.text)
        forms = [node for node in tree.nodes if node["tag"] == "form"]
        assert len(forms) == 1
        assert forms[0]["attrs"]["action"] == "/admin/login"
        assert forms[0]["attrs"]["method"] == "post"
        inputs = {node["attrs"].get("name"): node["attrs"] for node in tree.nodes
                  if node["tag"] == "input"}
        assert inputs["csrf_token"]["value"] == client.cookies.get("sideword_csrf")
        assert inputs["username"]["autocomplete"] == "username"
        assert "autofocus" in inputs["username"]
        assert inputs["password"]["autocomplete"] == "current-password"
        assert "admin.css" in response.text
        assert "client.css" not in response.text
        assert "appearance.js" not in response.text
        rejected = client.post("/admin/login", data={
            "csrf_token": inputs["csrf_token"]["value"],
            "username": "no-such-admin", "password": "incorrect-password",
        }, headers={"Origin": "https://testserver"})
        assert rejected.status_code == 401
        assert any(node["attrs"].get("class") == "alert" for node in Structure(rejected.text).nodes)
        assert "Invalid username or password." in rejected.text


@pytest.mark.parametrize("status, expected, action", [
    ("active", "Open it in a compatible Sideword client to activate.", "Open test web client"),
    ("expired", "This link expired. Ask your administrator for a fresh invite.", None),
    ("used", "This room is sealed. All participant slots are filled.",
     "Reconnect from your joined browser"),
    ("revoked", "This link was revoked by an administrator.", None),
    ("unknown", "This invite token does not exist.", None),
])
def test_invite_states_use_client_theme_and_preserve_status_messages(status, expected, action):
    request = Request({"type": "http", "method": "GET", "path": "/l/example-invite",
                       "headers": []})
    html = templates.env.get_template("landing.html").render(
        request=request, token="example-invite", status=status,
        details={"is_personal": True, "password_required": True},
    )
    assert expected in html
    assert "client.css" in html
    assert "admin.css" not in html
    assert html.index("appearance.js") < html.index("client.css")
    assert "Test client · unaudited" in html
    assert len([node for node in Structure(html).nodes
                if node["attrs"].get("name") == "appearance"]) == 3
    if action:
        assert action in html
        assert 'href="/client?invite=example-invite"' in html
    else:
        assert 'href="/client?invite=' not in html
    if status == "active":
        assert "private 1:1 chat" in html
        assert "Opening this page does not reserve a participant slot." in html
        assert "Ask the creator for it separately." in html
        assert 'class="invite-token"' in html
    else:
        assert 'class="invite-token"' not in html


def test_group_invite_preserves_type_and_optional_password_guidance():
    request = Request({"type": "http", "method": "GET", "path": "/l/example-invite",
                       "headers": []})
    html = templates.env.get_template("landing.html").render(
        request=request, token="example-invite", status="active",
        details={"is_personal": False, "password_required": False},
    )
    assert "<b>group</b> invite link" in html
    assert "The room also requires a phrase or password." not in html
