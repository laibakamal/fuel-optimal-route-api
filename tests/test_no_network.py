"""Meta-tests: prove the no-network guarantee is enforced, not merely intended.

The brief requires that no test hits the network. A comment saying so is worth
nothing; these tests demonstrate that an attempt actually fails.
"""
from __future__ import annotations

import socket

import pytest
import requests

from tests.conftest import NetworkAccessAttempted


def test_a_raw_socket_connection_is_blocked():
    with pytest.raises(NetworkAccessAttempted):
        socket.create_connection(("example.com", 80), timeout=1)


def test_a_requests_call_is_blocked():
    """requests goes through socket internally, so the guard catches it too."""
    with pytest.raises(Exception) as exc:
        requests.get("https://router.project-osrm.org/route/v1/driving/0,0;1,1", timeout=1)
    # requests wraps the underlying error; the guard's message must be in the chain.
    chain, seen = exc.value, []
    while chain is not None and len(seen) < 10:
        seen.append(str(chain))
        chain = chain.__cause__ or chain.__context__
    assert any("network connection" in message for message in seen), seen


def test_the_real_osrm_provider_cannot_reach_the_network():
    """If a test ever forgets to mock the provider, it fails loudly here rather
    than silently making a live call.

    The guard raises NetworkAccessAttempted, a RuntimeError, which deliberately is
    NOT a requests.RequestException - so it propagates straight out of the
    provider's failover handler instead of being swallowed and retried against the
    fallback endpoint. That is what makes the block unmissable.
    """
    from services.routing import OsrmRouteProvider

    provider = OsrmRouteProvider(
        "https://router.project-osrm.org", "https://routing.openstreetmap.de/routed-car"
    )
    with pytest.raises(NetworkAccessAttempted):
        provider.fetch((32.7767, -96.7970), (41.8781, -87.6298))
