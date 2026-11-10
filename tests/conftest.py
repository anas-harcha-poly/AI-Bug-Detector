"""Protects the developer's real configuration and services during tests.

1. Streamlit never reads the real .streamlit/secrets.toml, which may hold the real
   Anthropic key and the real budget database. Tests only see the secrets they set
   themselves (AppTest's at.secrets, or monkeypatched environment variables).
2. No test can open a network connection outside this computer. A test that forgot
   to replace Upstash or Anthropic by a fake fails, instead of reaching the real
   service with real secrets.
"""

import ipaddress
import socket
import tempfile
from pathlib import Path

import streamlit as st
from streamlit import config

_NO_SECRETS = Path(tempfile.mkdtemp(prefix="no-secrets-")) / "secrets.toml"
config.set_option("secrets.files", [str(_NO_SECRETS)])
st.secrets._reset()  # Forget the real file, if it was already read.


def _is_local(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


class NetworkBlockedError(OSError):
    pass


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _check(address) -> None:
    host = address[0] if isinstance(address, tuple) else None
    if isinstance(address, tuple) and not _is_local(host):
        raise NetworkBlockedError(f"Tests may not connect to {host!r}.")


def _connect(self, address):
    _check(address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check(address)
    return _real_connect_ex(self, address)


def _getaddrinfo(host, *args, **kwargs):
    if not _is_local(host):
        raise NetworkBlockedError(f"Tests may not look up {host!r}.")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.getaddrinfo = _getaddrinfo
