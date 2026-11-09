"""Where the app runs: on the owner's computer (local), or as a public website.

Local mode is the default, and works as before: the owner can turn on real AI review
with their own key (typed in the page), and Docker test runs.

Public mode is turned on explicitly with AI_BUG_DETECTOR_MODE=public. Visitors never
give a key: the AI review uses the site owner's key, read on the server from the
Streamlit secrets or from an environment variable. Docker test runs are always off.

Any other value of AI_BUG_DETECTOR_MODE gives the most restrictive behaviour: public
mode without paid AI, so a typo can never turn on more than intended.
"""

import os
from enum import StrEnum

MODE_VARIABLE = "AI_BUG_DETECTOR_MODE"
# Name of the site owner's key, in .streamlit/secrets.toml or the environment.
SERVER_KEY_NAME = "ANTHROPIC_API_KEY"
# Where the shared AI budget is kept: a Redis database with an Upstash-style REST API.
STORE_URL_NAME = "UPSTASH_REDIS_REST_URL"
STORE_TOKEN_NAME = "UPSTASH_REDIS_REST_TOKEN"


class Mode(StrEnum):
    LOCAL = "local"
    PUBLIC = "public"
    MISCONFIGURED = "misconfigured"  # Treated as public, without paid AI.


def deployment_mode() -> Mode:
    """Return the mode set by AI_BUG_DETECTOR_MODE, in the environment or secrets."""
    value = os.environ.get(MODE_VARIABLE)
    if value is None:
        value = _secret(MODE_VARIABLE)
    if value is None or not value.strip() or value.strip().lower() == "local":
        return Mode.LOCAL
    if value.strip().lower() == "public":
        return Mode.PUBLIC
    return Mode.MISCONFIGURED


def is_public() -> bool:
    """True whenever the app may be reachable by visitors: never trust them."""
    return deployment_mode() is not Mode.LOCAL


def server_api_key() -> str | None:
    """Return the site owner's key, in public mode only. Never store the result.

    Read again for each call, so it never ends up in st.session_state or any other
    place a visitor's session can reach. It is never shown or logged.
    """
    return server_setting(SERVER_KEY_NAME)


def server_key_configured() -> bool:
    return server_api_key() is not None


def server_setting(name: str) -> str | None:
    """Return a secret setting of a public site, or None. Never store the result.

    Read from the Streamlit secrets first, then from the environment. In local mode,
    or with a mode that is not valid, nothing is ever read.
    """
    if deployment_mode() is not Mode.PUBLIC:
        return None
    value = _secret(name)
    if value is None:
        value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _secret(name: str) -> str | None:
    """Read a Streamlit secret. Without a secrets file, Streamlit raises: None."""
    try:
        import streamlit as st

        value = st.secrets.get(name)
    except Exception:
        # No secrets file, or no Streamlit context: the secret is simply not set.
        return None
    return value if isinstance(value, str) else None
