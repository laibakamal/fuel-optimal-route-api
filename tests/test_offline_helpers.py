"""The offline pipeline helpers: downloader, geocode cache, route fixtures.

All network is mocked. These exist because each of them has a failure mode that
would silently corrupt results: a truncated download that looks complete, a
geocode cache that reaches the network when told not to, or a fixture provider
that claims an external call it never made.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from services.download import download_if_missing
from services.geocode_fallback import CachedNominatim
from services.route_fixtures import FixtureBackedProvider
from services.routing import Route, RoutePoint, RoutingError


class FakeUrlopen:
    """Context-manager stand-in for urllib.request.urlopen."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self._read = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=None):
        if self._read:
            return b""
        self._read = True
        return self.payload


# --------------------------------------------------------------------------
# download_if_missing
# --------------------------------------------------------------------------

def test_download_writes_the_file(tmp_path, monkeypatch):
    calls = []

    def fake(request, timeout=None):
        calls.append(request.full_url)
        return FakeUrlopen(b"payload")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    target = tmp_path / "x.zip"
    download_if_missing("https://example.test/x.zip", target, "ua/1.0")
    assert target.read_bytes() == b"payload"
    assert calls == ["https://example.test/x.zip"]


def test_download_is_skipped_when_the_file_exists(tmp_path, monkeypatch):
    target = tmp_path / "x.zip"
    target.write_bytes(b"already here")

    def fail(*args, **kwargs):
        raise AssertionError("should not download when the file exists")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    download_if_missing("https://example.test/x.zip", target, "ua/1.0")
    assert target.read_bytes() == b"already here"


def test_download_sends_the_user_agent(tmp_path, monkeypatch):
    captured = {}

    def fake(request, timeout=None):
        captured["ua"] = request.get_header("User-agent")
        return FakeUrlopen(b"x")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    download_if_missing("https://example.test/x", tmp_path / "x", "fuel-route/9.9")
    assert captured["ua"] == "fuel-route/9.9"


def test_interrupted_download_leaves_no_complete_looking_file(tmp_path, monkeypatch):
    """A truncated archive that a later run treats as complete is the dangerous
    failure here, so the download goes to .part and is renamed only on success."""

    class Exploding(FakeUrlopen):
        def read(self, size=None):
            raise OSError("connection reset")

    monkeypatch.setattr("urllib.request.urlopen", lambda r, timeout=None: Exploding(b""))
    target = tmp_path / "x.zip"
    with pytest.raises(OSError):
        download_if_missing("https://example.test/x.zip", target, "ua")
    assert not target.exists()


# --------------------------------------------------------------------------
# CachedNominatim
# --------------------------------------------------------------------------

def test_cached_lookup_makes_no_network_call(tmp_path, monkeypatch):
    cache = tmp_path / "geocode.json"
    cache.write_text(json.dumps({"DALLAS|TX": [32.78, -96.80]}))

    def fail(*args, **kwargs):
        raise AssertionError("cache hit must not reach the network")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    geocoder = CachedNominatim(cache, "ua")
    assert geocoder.lookup("Dallas", "TX") == pytest.approx((32.78, -96.80))
    assert geocoder.calls_made == 0
    assert geocoder.cache_hits == 1


def test_offline_only_never_reaches_the_network(tmp_path, monkeypatch):
    """This is the bug the --offline flag had: it must not silently fetch."""

    def fail(*args, **kwargs):
        raise AssertionError("offline_only must not reach the network")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    geocoder = CachedNominatim(tmp_path / "none.json", "ua", offline_only=True)
    assert geocoder.lookup("Nowhere", "ZZ") is None
    assert geocoder.calls_made == 0


def test_network_lookup_is_cached_and_flushed(tmp_path, monkeypatch):
    payload = json.dumps([{"lat": "40.5", "lon": "-80.2"}]).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda r, timeout=None: FakeUrlopen(payload)
    )
    monkeypatch.setattr("services.geocode_fallback.MIN_SECONDS_BETWEEN_CALLS", 0.0)
    cache = tmp_path / "geocode.json"
    geocoder = CachedNominatim(cache, "ua")
    assert geocoder.lookup("Crescent", "PA") == pytest.approx((40.5, -80.2))
    assert geocoder.calls_made == 1
    # A repeat must come from memory, not the network.
    geocoder.lookup("Crescent", "PA")
    assert geocoder.calls_made == 1
    geocoder.flush()
    assert json.loads(cache.read_text())["CRESCENT|PA"] == [40.5, -80.2]


def test_a_network_failure_returns_none_rather_than_aborting_the_run(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("dns failure")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setattr("services.geocode_fallback.MIN_SECONDS_BETWEEN_CALLS", 0.0)
    geocoder = CachedNominatim(tmp_path / "g.json", "ua")
    assert geocoder.lookup("X", "TX") is None


def test_a_corrupt_cache_file_is_tolerated(tmp_path, monkeypatch):
    cache = tmp_path / "geocode.json"
    cache.write_text("{not json")
    monkeypatch.setattr("urllib.request.urlopen", lambda r, timeout=None: FakeUrlopen(b"[]"))
    monkeypatch.setattr("services.geocode_fallback.MIN_SECONDS_BETWEEN_CALLS", 0.0)
    geocoder = CachedNominatim(cache, "ua")
    assert geocoder.lookup("X", "TX") is None


# --------------------------------------------------------------------------
# FixtureBackedProvider
# --------------------------------------------------------------------------

def osrm_payload():
    return {
        "code": "Ok",
        "routes": [{
            "distance": 1609.344, "duration": 60.0,
            "geometry": {"type": "LineString",
                         "coordinates": [[-100.0, 35.0], [-100.0, 36.0]]},
            "legs": [{"annotation": {"distance": [1609.344]}}],
        }],
    }


class RecordingProvider:
    def __init__(self):
        self.calls = 0
        self.last_payload = osrm_payload()

    def fetch(self, origin, destination):
        self.calls += 1
        return Route(
            points=[RoutePoint(35.0, -100.0, 0.0), RoutePoint(36.0, -100.0, 1.0)],
            total_miles=1.0, duration_seconds=60.0, provider="inner", external_calls=1,
        )


def test_first_fetch_records_a_fixture_then_replays_it(tmp_path):
    inner = RecordingProvider()
    provider = FixtureBackedProvider(inner, tmp_path / "fx")
    provider.fetch((35.0, -100.0), (36.0, -100.0))
    assert inner.calls == 1
    assert provider.network_calls == 1

    second = provider.fetch((35.0, -100.0), (36.0, -100.0))
    assert inner.calls == 1, "second fetch must come from the fixture"
    assert provider.fixture_hits == 1
    # A replayed route cost nothing and must not claim a call it never made.
    assert second.external_calls == 0
    assert second.provider == "fixture"


def test_offline_mode_errors_on_a_missing_fixture(tmp_path):
    inner = RecordingProvider()
    provider = FixtureBackedProvider(inner, tmp_path / "fx", offline=True)
    with pytest.raises(RoutingError, match="offline mode"):
        provider.fetch((35.0, -100.0), (36.0, -100.0))
    assert inner.calls == 0


def test_different_endpoints_get_different_fixtures(tmp_path):
    inner = RecordingProvider()
    provider = FixtureBackedProvider(inner, tmp_path / "fx")
    provider.fetch((35.0, -100.0), (36.0, -100.0))
    provider.fetch((40.0, -90.0), (41.0, -90.0))
    assert inner.calls == 2
    assert len(list((tmp_path / "fx").glob("*.json"))) == 2
