"""Check the local container listener without external tools or proxy settings."""

from http.client import HTTPConnection, HTTPException


def main():
    connection = HTTPConnection("127.0.0.1", 8000, timeout=3)
    try:
        connection.request("GET", "/health")
        return int(connection.getresponse().status != 200)
    except (OSError, HTTPException):
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
