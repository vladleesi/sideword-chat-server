"""Public asset transport optimizations must not cache or compress private data."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
CACHE = "public, max-age=3600, must-revalidate"


@pytest.fixture
def client():
    with TestClient(create_app(), base_url="https://testserver") as value:
        yield value


@pytest.mark.parametrize("path", [
    "/static/client.js?v=28", "/static/client.css?v=15",
    "/static/client-protocol.js?v=3", "/static/client.js",
    "/static/fonts/IBMPlexSans-Regular.woff2",
])
def test_public_assets_are_cacheable_and_buffered(client, path):
    response = client.get(path, headers={"Accept-Encoding": "identity"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == CACHE
    assert response.headers["x-accel-buffering"] == "yes"
    assert "immutable" not in response.headers["cache-control"]
    assert "set-cookie" not in response.headers
    asset = path.split("?", 1)[0].removeprefix("/static/")
    assert response.content == (STATIC / asset).read_bytes()


@pytest.mark.parametrize("name", ["client.js", "client.css"])
def test_gzip_reduces_public_asset_transfers_without_changing_content(client, name):
    expected = (STATIC / name).read_bytes()
    compressed = client.get(f"/static/{name}", headers={"Accept-Encoding": "gzip"})
    assert compressed.status_code == 200
    assert compressed.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in compressed.headers["vary"]
    assert compressed.content == expected  # HTTPX decodes the actual gzip stream.
    assert compressed.num_bytes_downloaded < len(expected) / 2
    identity = client.get(f"/static/{name}", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in identity.headers
    assert identity.content == expected


@pytest.mark.parametrize("validator", ["etag", "last-modified"])
@pytest.mark.parametrize("encoding", ["gzip", "identity"])
def test_conditional_requests_preserve_asset_cache_policy(client, validator, encoding):
    path = "/static/client.js?v=28"
    original = client.get(path, headers={"Accept-Encoding": encoding})
    header = "If-None-Match" if validator == "etag" else "If-Modified-Since"
    response = client.get(path, headers={header: original.headers[validator],
                                        "Accept-Encoding": encoding})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["cache-control"] == CACHE
    assert response.headers["x-accel-buffering"] == "yes"
    assert "Accept-Encoding" in response.headers["vary"]


def test_head_and_range_keep_file_semantics(client):
    expected = (STATIC / "client.js").read_bytes()
    head = client.head("/static/client.js", headers={"Accept-Encoding": "identity"})
    assert head.status_code == 200
    assert head.content == b""
    assert int(head.headers["content-length"]) == len(expected)
    assert head.headers["cache-control"] == CACHE
    partial = client.get("/static/client.js", headers={"Accept-Encoding": "identity",
                                                       "Range": "bytes=0-31"})
    assert partial.status_code == 206
    assert partial.content == expected[:32]
    assert partial.headers["content-range"] == f"bytes 0-31/{len(expected)}"
    assert partial.headers["cache-control"] == CACHE
    compressed_range = client.get("/static/client.js", headers={"Accept-Encoding": "gzip",
                                                                "Range": "bytes=0-65535"})
    assert compressed_range.status_code == 206
    assert "content-encoding" not in compressed_range.headers
    assert compressed_range.content == expected[:65536]


@pytest.mark.parametrize("path", [
    "/client", "/client?invite=test-only-placeholder", "/health",
    "/api/v1/me", "/admin/login", "/l/test-only-placeholder",
    "/static/missing.js", "/static/", "/static/%2e%2e/main.py",
])
def test_private_responses_and_errors_never_use_public_asset_policy(client, path):
    response = client.get(path, headers={"Accept-Encoding": "gzip"}, follow_redirects=False)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "x-accel-buffering" not in response.headers
    assert "content-encoding" not in response.headers
    if path.startswith("/client"):
        assert "default-src 'none'" in response.headers["content-security-policy"]


def test_static_validation_errors_are_not_cacheable(client):
    response = client.get("/static/client.js", headers={"Range": "bytes=999999999-"})
    assert response.status_code == 416
    assert response.headers["cache-control"] == "no-store"
    assert "x-accel-buffering" not in response.headers
