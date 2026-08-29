"""Tests for market data TTLCache."""

import time

import pytest

from fin_analyse.market.cache import TTLCache


class TestTTLCache:
    @pytest.fixture
    def cache(self):
        return TTLCache()

    def test_set_and_get(self, cache):
        cache.set("key1", "value1", ttl_seconds=60)
        assert cache.get("key1") == "value1"

    def test_miss_returns_none(self, cache):
        assert cache.get("nonexistent") is None

    def test_ttl_expiry(self, cache):
        cache.set("key1", "value1", ttl_seconds=0.01)
        time.sleep(0.03)
        assert cache.get("key1") is None

    def test_invalidate(self, cache):
        cache.set("akshare:quote:000001", 1, ttl_seconds=60)
        cache.set("akshare:history:000001", 2, ttl_seconds=60)
        cache.invalidate("akshare:quote")
        assert cache.get("akshare:quote:000001") is None
        assert cache.get("akshare:history:000001") == 2

    def test_overwrite(self, cache):
        cache.set("key1", "old", ttl_seconds=60)
        cache.set("key1", "new", ttl_seconds=60)
        assert cache.get("key1") == "new"

    def test_make_key(self, cache):
        key = cache.make_key("akshare", "get_history", "000001", "120")
        assert "akshare" in key
        assert "get_history" in key
        assert "000001" in key

    def test_make_key_no_params(self, cache):
        key = cache.make_key("akshare", "get_quote", "000001")
        assert key == "akshare:get_quote:000001"
