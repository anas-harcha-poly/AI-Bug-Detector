"""A fake Upstash database for tests: nothing leaves the computer.

Requests are handled like the real REST API (a JSON array in, {"result": ...} or
{"error": ...} out), and commands run on fakeredis, which executes the real Lua
scripts. Like a real Redis server, it runs one command at a time.
"""

import json
import threading

import fakeredis
import redis

URL = "https://fake-budget.example.test"
TOKEN = "fake-store-token-SECRET"


def _plain(value):
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


class FakeUpstash:
    def __init__(self) -> None:
        self.server = fakeredis.FakeServer()
        # For tests to look inside the database.
        self.redis = fakeredis.FakeRedis(server=self.server)
        # For requests: raw answers, as Upstash sends them ("OK", not True).
        self._raw = fakeredis.FakeRedis(server=self.server)
        self._raw.response_callbacks.clear()
        self._lock = threading.Lock()
        self.requests: list[tuple[str, str, list]] = []
        self.down = False  # True: every request fails, like a network outage.

    def __call__(self, url: str, token: str, body: bytes, timeout: float):
        command = json.loads(body)
        self.requests.append((url, token, command))
        if self.down:
            raise OSError("connection refused")
        if url != URL or token != TOKEN:
            return 401, b'{"error":"Unauthorized"}'
        with self._lock:
            try:
                result = self._raw.execute_command(*command)
            except redis.ResponseError as error:
                return 400, json.dumps({"error": f"ERR {error}"}).encode()
        return 200, json.dumps({"result": _plain(result)}).encode()

    def commands(self) -> list[str]:
        return [command[0] for _, _, command in self.requests]
