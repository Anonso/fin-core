"""Tests for utils/ids.py."""

from fin_analyse.utils.ids import stable_id


def test_stable_id_deterministic():
    assert stable_id("a", "b", prefix="test:") == stable_id("a", "b", prefix="test:")


def test_stable_id_different_inputs():
    assert stable_id("a") != stable_id("b")


def test_stable_id_prefix():
    result = stable_id("hello", prefix="claim:")
    assert result.startswith("claim:")


def test_stable_id_default_length():
    result = stable_id("hello")
    # 12 hex chars + no prefix
    assert len(result) == 12


def test_stable_id_custom_length():
    result = stable_id("hello", digest_len=8)
    assert len(result) == 8
