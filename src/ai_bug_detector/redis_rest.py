"""A minimal client for Redis over HTTPS, in the format of the Upstash REST API.

Each command is one POST request whose body is a JSON array, such as
["GET", "key"], with the token in an Authorization header. The answer is
{"result": ...} or {"error": "..."}. Only the standard library is used.

The URL and the token are secrets: they never appear in an error message.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

TIMEOUT_SECONDS = 5
_MAX_RESPONSE_BYTES = 1_000_000

# (url, token, body, timeout) -> (HTTP status, response body)
Transport = Callable[[str, str, bytes, float], tuple[int, bytes]]


class StoreError(Exception):
    """The store could not be used. The message never contains the URL or token."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could send the token to another server, so none is followed."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def https_post(url: str, token: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    """Send one request. Used for real, and replaced by a fake one in tests."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        # The body of an error is not needed: the status code is enough.
        error.close()
        return error.code, b""


class RedisRest:
    """Sends Redis commands to one database, over HTTPS only."""

    def __init__(self, url: str, token: str, transport: Transport | None = None):
        parts = urllib.parse.urlsplit(url.strip())
        if parts.scheme != "https" or not parts.hostname or not token.strip():
            raise StoreError("The storage settings are not valid.")
        self._url = url.strip()
        self._token = token.strip()
        self._transport = transport

    def __repr__(self) -> str:
        return "RedisRest(<hidden>)"  # Never shows the URL or the token.

    def command(self, *args: str | int) -> object:
        """Run one command and return its result."""
        body = json.dumps([str(arg) for arg in args]).encode("utf-8")
        transport = self._transport or https_post
        try:
            status, raw = transport(self._url, self._token, body, TIMEOUT_SECONDS)
        except Exception:
            raise StoreError("The storage could not be reached.") from None
        try:
            answer = json.loads(raw)
        except (TypeError, ValueError):
            answer = None
        if status != 200 or not isinstance(answer, dict) or "error" in answer:
            raise StoreError(f"The storage refused the request (HTTP {status}).")
        if "result" not in answer:
            raise StoreError("The storage gave an answer that is not valid.")
        return answer["result"]
