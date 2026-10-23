"""Runtime health and tooling removal must preserve container verification."""

import threading
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from scripts import healthcheck

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("status,expected", [(200, 0), (204, 1), (302, 1), (503, 1)])
def test_health_probe_uses_local_http_without_proxies(monkeypatch, capsys, status, expected):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def connect(host, port, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 8000, 3)
        return HTTPConnection(host, server.server_port, timeout=timeout)

    monkeypatch.setattr(healthcheck, "HTTPConnection", connect)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    try:
        assert healthcheck.main() == expected
        assert requests == ["/health"]
        assert capsys.readouterr().out == ""
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.mark.parametrize("error", [TimeoutError, ConnectionRefusedError, HTTPException])
def test_health_probe_fails_quietly_and_closes_on_transport_error(monkeypatch, capsys, error):
    closed = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args):
            raise error("synthetic-private")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(healthcheck, "HTTPConnection", Connection)
    assert healthcheck.main() == 1
    assert closed == [True]
    assert capsys.readouterr() == ("", "")


def test_container_checks_dependencies_before_removing_unneeded_tools():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert dockerfile.index("pip check") < dockerfile.index("pip uninstall --yes pip")
    assert "sysconfig.get_path('stdlib')) / 'ensurepip'" in dockerfile
    assert "apt-get upgrade -y" in dockerfile
    assert "curl" not in dockerfile
    assert 'CMD ["python", "-m", "scripts.healthcheck"]' in dockerfile
    assert "scripts/healthcheck.py ./scripts/healthcheck.py" in dockerfile
    assert "scripts/create_admin.py ./scripts/create_admin.py" in dockerfile
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "healthcheck" not in compose["services"]["sideword"]
