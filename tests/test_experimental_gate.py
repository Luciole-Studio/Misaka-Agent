"""pi coding-agent's ``test/experimental.test.ts``."""

from __future__ import annotations

from misaka.core.experimental import areExperimentalFeaturesEnabled


def test_returns_false_when_pi_experimental_is_unset(monkeypatch):
    monkeypatch.delenv("PI_EXPERIMENTAL", raising=False)

    assert areExperimentalFeaturesEnabled() is False


def test_returns_false_when_pi_experimental_is_empty(monkeypatch):
    monkeypatch.setenv("PI_EXPERIMENTAL", "")

    assert areExperimentalFeaturesEnabled() is False


def test_returns_true_when_pi_experimental_is_set_to_1(monkeypatch):
    monkeypatch.setenv("PI_EXPERIMENTAL", "1")

    assert areExperimentalFeaturesEnabled() is True


def test_returns_false_when_pi_experimental_is_set_to_0(monkeypatch):
    monkeypatch.setenv("PI_EXPERIMENTAL", "0")

    assert areExperimentalFeaturesEnabled() is False


def test_returns_false_when_pi_experimental_is_set_to_a_non_1_value(monkeypatch):
    monkeypatch.setenv("PI_EXPERIMENTAL", "true")

    assert areExperimentalFeaturesEnabled() is False
