"""Existing functional tests follow the browser's CSRF bootstrap flow."""

from fastapi.testclient import TestClient as BaseClient


class TestClient(BaseClient):
    __test__ = False

    def __init__(self, app, **kwargs):
        kwargs.setdefault("base_url", "https://testserver")
        super().__init__(app, **kwargs)

    def request(self, method, url, **kwargs):
        if method.upper() not in {"GET", "HEAD", "OPTIONS"} and str(url).startswith("/admin"):
            self.get("/admin/login", follow_redirects=False)
            kwargs["headers"] = {"X-CSRF-Token": self.cookies.get("sideword_csrf"),
                                 **(kwargs.get("headers") or {})}
        return super().request(method, url, **kwargs)
