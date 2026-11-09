"""pi v1.5.0 ``test/paths.test.ts`` ("path comparison"): ``getPathComparisonKey``, ``pathsEqual``,
``isPathWithin`` and ``shortenHomePath``."""

from __future__ import annotations

import sys

import pytest

from misaka.utils.paths import (
    getPathComparisonKey,
    isPathWithin,
    pathsEqual,
    shortenHomePath,
)


@pytest.fixture
def simulatePlatform(monkeypatch):
    def simulate(platform: str) -> None:
        monkeypatch.setattr(sys, "platform", platform)

    return simulate


# Regression test for https://github.com/earendil-works/pi/issues/10488
def test_treats_drive_letters_and_path_components_case_insensitively_on_windows(simulatePlatform):
    simulatePlatform("win32")
    assert getPathComparisonKey("c:\\Users\\me\\.agents\\skills") == getPathComparisonKey(
        "C:\\Users\\Me\\.agents\\skills"
    )
    assert pathsEqual("c:\\Users\\me", "C:/Users/Me/") is True
    assert pathsEqual("C:\\Users\\me", "D:\\Users\\me") is False
    assert isPathWithin("C:\\Users\\Me", "c:\\users\\me\\project") is True
    assert isPathWithin("C:\\Users\\Me", "C:\\Users\\Me2") is False
    assert isPathWithin("C:\\", "c:\\Users") is True
    assert isPathWithin("\\\\server\\share", "\\\\SERVER\\Share\\folder") is True


def test_keeps_casing_on_case_sensitive_platforms(simulatePlatform):
    simulatePlatform("linux")
    assert pathsEqual("/home/me", "/home/Me") is False
    assert isPathWithin("/home/me", "/home/Me/project") is False


def test_is_path_within_matches_whole_path_segments(simulatePlatform):
    simulatePlatform("linux")
    assert isPathWithin("/home/me", "/home/me") is True
    assert isPathWithin("/home/me", "/home/me/project") is True
    assert isPathWithin("/home/me/", "/home/me/project") is True
    assert isPathWithin("/home/me", "/home/me2") is False
    assert isPathWithin("/", "/home") is True


def test_shorten_home_path_replaces_only_a_leading_home_directory(simulatePlatform):
    simulatePlatform("linux")
    assert shortenHomePath("/home/me", "/home/me") == "~"
    assert shortenHomePath("/home/me/project", "/home/me") == "~/project"
    assert shortenHomePath("/home/me2/project", "/home/me") == "/home/me2/project"
    assert shortenHomePath("/home/Me/project", "/home/me") == "/home/Me/project"
    assert shortenHomePath("/etc/passwd", "/") == "~/etc/passwd"
    assert shortenHomePath("home/me/project", "/home/me") == "home/me/project"
    assert shortenHomePath("/home/me/project", "") == "/home/me/project"


def test_shorten_home_path_ignores_casing_and_keeps_separators_on_windows(simulatePlatform):
    simulatePlatform("win32")
    assert shortenHomePath("c:\\users\\me\\project", "C:\\Users\\Me") == "~\\project"
    assert shortenHomePath("\\\\server\\share\\folder", "\\\\server\\share") == "~\\folder"
