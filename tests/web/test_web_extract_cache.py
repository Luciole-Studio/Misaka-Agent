"""The disk-backed extract cache: what it stores, what it refuses, and what it survives.

Ported from the extract half of Hermes' ``tests/tools/test_web_result_cache.py``. Nothing
here touches the network; the cache sits below every safety gate and above the paid vendor
call, so what matters is hit/miss semantics, key composition, and the fact that a hostile
or merely broken index on disk reads as a miss rather than an exception.

``HOME`` is redirected into ``tmp_path``, so the cache directory, the entry files and
``web.json`` all live inside the test's own tmpdir and no test can reach ``~/.misaka``.
"""

from __future__ import annotations

import json
import re
import stat
import time

import pytest

from misaka.config.product import CFG
from misaka.core.web import cache


@pytest.fixture(autouse=True)
def web_home(monkeypatch, tmp_path):
    """A throwaway ``~/.misaka``: config path and cache directory both under tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / ".misaka" / "web.json"
    monkeypatch.setitem(CFG, "web_config", str(config_path))
    monkeypatch.setitem(CFG, "web_cache", str(tmp_path / ".misaka" / "cache" / "web"))
    return config_path


@pytest.fixture
def cache_dir(tmp_path):
    """Where :func:`misaka.core.web.cache._cache_dir` resolves under this HOME."""
    return tmp_path / ".misaka" / "cache" / "web"


def write_config(web_home, **values):
    """Write ``web.json`` the way the CLI writer would, then let the cache read it back."""
    web_home.parent.mkdir(parents=True, exist_ok=True)
    web_home.write_text(json.dumps(values), encoding="utf-8")


def index_of(cache_dir) -> dict:
    return json.loads((cache_dir / cache._INDEX_FILENAME).read_text(encoding="utf-8"))


def write_index(cache_dir, index: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / cache._INDEX_FILENAME).write_text(json.dumps(index), encoding="utf-8")


# ── where it lives ────────────────────────────────────────────────────────────


def test_cache_lives_under_the_misaka_home_cache_dir(cache_dir):
    """``~/.misaka/cache/<subsystem>/``, resolved at call time so a moved HOME is honoured."""
    assert cache._cache_dir() == cache_dir
    assert cache_dir.is_dir()


def test_entry_file_name_carries_host_and_the_whole_key(cache_dir):
    cache.extract_cache_put("https://docs.example.com/a", "text", format="markdown")
    names = [path.name for path in cache_dir.glob("*.cache.json")]
    assert len(names) == 1
    assert re.fullmatch(r"docs\.example\.com-[0-9a-f]{16}\.cache\.json", names[0])


def test_entry_files_are_world_readable_not_private(cache_dir):
    """Provider material and URLs can be account-private; both cache files are 0600."""
    cache.extract_cache_put("https://example.com/a", "text")
    written = [*cache_dir.glob("*.cache.json"), cache_dir / cache._INDEX_FILENAME]
    assert [stat.S_IMODE(path.stat().st_mode) for path in written] == [0o600, 0o600]


# ── round trip and expiry ─────────────────────────────────────────────────────


def test_roundtrip():
    cache.extract_cache_put("https://example.com/a", "hello world", title="T")
    hit = cache.extract_cache_get("https://example.com/a")
    assert hit == {
        "url": "https://example.com/a",
        "title": "T",
        "content": "hello world",
        "error": None,
        "cached": True,
        "metadata": {"sourceURL": "https://example.com/a", "served_by": ""},
    }


def test_entry_older_than_the_ttl_is_a_miss(cache_dir):
    """Aged on disk rather than by moving the clock: the TTL is read from web.json."""
    cache.extract_cache_put("https://e.com", "x")
    index = index_of(cache_dir)
    for entry in index.values():
        entry["fetched_at"] = time.time() - 3600  # default TTL is 20 minutes
    write_index(cache_dir, index)
    assert cache.extract_cache_get("https://e.com") is None


def test_entry_inside_the_ttl_survives_a_reread(cache_dir):
    cache.extract_cache_put("https://e.com", "x")
    index = index_of(cache_dir)
    for entry in index.values():
        entry["fetched_at"] = time.time() - 60
    write_index(cache_dir, index)
    assert cache.extract_cache_get("https://e.com")["content"] == "x"


# ── the key is (url, format, provider) ────────────────────────────────────────


def test_format_participates_in_the_key():
    cache.extract_cache_put("https://e.com", "md content", format="markdown")
    assert cache.extract_cache_get("https://e.com", format="html") is None
    assert cache.extract_cache_get("https://e.com", format="markdown") is not None


def test_an_unset_format_shares_the_markdown_entry():
    """``None`` means "the default", and the default is markdown -- not a third key."""
    cache.extract_cache_put("https://e.com", "md content", format="markdown")
    assert cache.extract_cache_get("https://e.com")["content"] == "md content"


def test_formats_do_not_overwrite_each_other(cache_dir):
    """Hermes' own review finding: a URL-only key let the later write clobber the earlier.

    The entry files must be distinct too, which is why they are not the URL-keyed file the
    truncate-store writes.
    """
    cache.extract_cache_put("https://e.com/page", "# MARKDOWN", format="markdown")
    cache.extract_cache_put("https://e.com/page", "<h1>HTML</h1>", format="html")
    assert cache.extract_cache_get("https://e.com/page", format="markdown")["content"] == "# MARKDOWN"
    assert cache.extract_cache_get("https://e.com/page", format="html")["content"] == "<h1>HTML</h1>"
    assert len(list(cache_dir.glob("*.cache.json"))) == 2


def test_provider_participates_in_the_key():
    """Switching extract backends inside the TTL must not serve the old one's rendering."""
    cache.extract_cache_put("https://e.com/p", "firecrawl version", provider="firecrawl")
    assert cache.extract_cache_get("https://e.com/p", provider="keenable") is None
    assert cache.extract_cache_get("https://e.com/p", provider="firecrawl")["content"] == (
        "firecrawl version"
    )


# ── what is never stored ──────────────────────────────────────────────────────


def test_an_oversized_page_is_not_cached(cache_dir):
    """A capped copy served back would look whole and silently lose its tail."""
    cache.extract_cache_put("https://big.com", "x" * (cache.MAX_STORED_TEXT_CHARS + 1))
    assert cache.extract_cache_get("https://big.com") is None
    assert list(cache_dir.glob("*.cache.json")) == []


def test_a_page_at_the_ceiling_is_still_cached():
    cache.extract_cache_put("https://big.com", "x" * cache.MAX_STORED_TEXT_CHARS)
    assert cache.extract_cache_get("https://big.com") is not None


def test_empty_content_is_not_cached(cache_dir):
    cache.extract_cache_put("https://empty.com", "")
    assert list(cache_dir.glob("*.cache.json")) == []


@pytest.mark.parametrize("url", [
    "http://localhost:3000/app",
    "http://localhost:5173",                # a vite dev server
    "http://preview.localhost/artifact",
    "http://myapp.local/",
    "http://devbox/page",                   # a single-label LAN name
    "http://127.0.0.1:8080/preview",
    "http://[::1]:3000/",
    "http://192.168.1.44/dashboard",
    "http://10.0.0.5:8000/api/docs",
    "http://172.16.0.9/",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0:8000/",
    "not a url at all",
])
def test_local_and_private_urls_are_never_cached(url, cache_dir):
    """Dev servers and LAN previews change on every save: freshness beats dedup."""
    cache.extract_cache_put(url, "stale build output")
    assert cache.extract_cache_get(url) is None
    assert list(cache_dir.glob("*.cache.json")) == []


@pytest.mark.parametrize("url", [
    "https://example.com/page",
    "https://docs.python.org/3/",
    "https://93.184.216.34/page",           # a public literal address
])
def test_public_urls_still_cache(url):
    cache.extract_cache_put(url, "public content")
    assert cache.extract_cache_get(url)["content"] == "public content"


class TestCacheExemptHosts:
    """``cache_exempt_hosts``: staging and tunnel sites on public DNS, always fetched live."""

    @pytest.mark.parametrize("pattern,url", [
        ("mysite.vercel.app", "https://mysite.vercel.app/page"),
        ("MYSITE.VERCEL.APP", "https://mysite.vercel.app/page"),      # case-insensitive
        ("*.ngrok-free.app", "https://abc123.ngrok-free.app/"),
        ("mysite.dev", "https://preview.mysite.dev/build/7"),         # bare-domain suffix
        ("mysite.dev", "https://mysite.dev/"),                        # and exact
    ])
    def test_an_exempt_host_is_never_cached(self, web_home, cache_dir, pattern, url):
        write_config(web_home, cache_exempt_hosts=[pattern])
        cache.extract_cache_put(url, "stale staging build")
        assert cache.extract_cache_get(url) is None
        assert list(cache_dir.glob("*.cache.json")) == []

    def test_a_non_matching_host_still_caches(self, web_home):
        write_config(web_home, cache_exempt_hosts=["mysite.vercel.app"])
        cache.extract_cache_put("https://docs.python.org/3/", "cached fine")
        assert cache.extract_cache_get("https://docs.python.org/3/") is not None

    def test_a_suffix_cannot_match_a_lookalike_domain(self, web_home):
        """``mysite.dev`` must not exempt ``evilmysite.dev``: matching is on label boundaries."""
        write_config(web_home, cache_exempt_hosts=["mysite.dev"])
        cache.extract_cache_put("https://evilmysite.dev/x", "content")
        assert cache.extract_cache_get("https://evilmysite.dev/x") is not None

    @pytest.mark.parametrize("value", ["not-a-list", 17, {}, [""], []])
    def test_a_garbage_config_fails_open_to_caching(self, web_home, value):
        write_config(web_home, cache_exempt_hosts=value)
        cache.extract_cache_put("https://example.com/a", "content")
        assert cache.extract_cache_get("https://example.com/a") is not None

    def test_the_exemption_applies_at_get_time_too(self, web_home):
        """Adding an exemption mid-TTL takes effect on the next lookup, not after it."""
        cache.extract_cache_put("https://mysite.vercel.app/p", "old build")
        write_config(web_home, cache_exempt_hosts=["mysite.vercel.app"])
        assert cache.extract_cache_get("https://mysite.vercel.app/p") is None


# ── a hostile or broken index ─────────────────────────────────────────────────


def test_an_index_path_outside_the_cache_dir_is_a_miss(cache_dir, tmp_path):
    """The index is plain JSON: a tampered entry must not become an arbitrary file read."""
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    write_index(cache_dir, {
        cache._url_digest("https://evil.com", None): {
            "url": "https://evil.com",
            "file": str(outside),
            "title": "",
            "fetched_at": time.time(),
        }
    })
    assert cache.extract_cache_get("https://evil.com") is None


def test_a_symlink_out_of_the_cache_dir_is_a_miss(cache_dir, tmp_path):
    """Containment is checked on the resolved path, so a symlink cannot step out either."""
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    cache_dir.mkdir(parents=True, exist_ok=True)
    link = cache_dir / "innocent.cache.json"
    link.symlink_to(outside)
    write_index(cache_dir, {
        cache._url_digest("https://evil.com", None): {
            "url": "https://evil.com",
            "file": str(link),
            "title": "",
            "fetched_at": time.time(),
        }
    })
    assert cache.extract_cache_get("https://evil.com") is None


def test_a_pruned_entry_file_is_a_miss(cache_dir):
    write_index(cache_dir, {
        cache._url_digest("https://gone.com", None): {
            "url": "https://gone.com",
            "file": str(cache_dir / "pruned.cache.json"),
            "title": "",
            "fetched_at": time.time(),
        }
    })
    assert cache.extract_cache_get("https://gone.com") is None


def test_a_corrupt_index_reads_as_an_empty_cache(cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / cache._INDEX_FILENAME).write_text("{not json", encoding="utf-8")
    assert cache.extract_cache_get("https://any.com") is None


@pytest.mark.parametrize("entry", [
    "not a mapping",
    {"file": "x", "fetched_at": "soon"},              # unparseable timestamp
    {"file": None, "fetched_at": 0},
    {},
])
def test_a_malformed_index_entry_is_a_miss_not_an_exception(cache_dir, entry):
    write_index(cache_dir, {cache._url_digest("https://weird.com", None): entry})
    assert cache.extract_cache_get("https://weird.com") is None


def test_a_non_object_index_reads_as_empty(cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / cache._INDEX_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
    assert cache.extract_cache_get("https://any.com") is None


def test_an_index_too_deep_to_parse_reads_as_an_empty_cache(cache_dir):
    """``RecursionError`` is not a ``ValueError``, and the index is a file anyone can write.

    A 60000-deep array of ``[`` blows the parser's stack rather than raising
    ``JSONDecodeError``. It reaches the caller as the same miss every other corrupt index
    does; narrowing the handler to the *documented* decode error let this one out.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    nested = "[" * 60_000 + "]" * 60_000
    (cache_dir / cache._INDEX_FILENAME).write_text(nested, encoding="utf-8")
    assert cache._load_index() == {}
    assert cache.extract_cache_get("https://any.com") is None


def test_an_undeterminable_home_is_a_miss_not_a_failed_extraction(monkeypatch):
    """No ``HOME`` and no passwd entry: ``Path.home()`` raises out of the path expansion.

    A container run as ``--user 1000:1000``, an ``env -i`` subprocess. There is no cache
    directory to be had, which is the None :func:`cache._cache_dir` already documents --
    but a lookup that *raised* it would surface through ``web_extract``'s outer handler as
    ``Error extracting content: Could not determine home directory.`` for a batch of URLs
    that never needed the cache.
    """
    monkeypatch.setitem(CFG, "web_cache", "~/.misaka/cache/web")
    monkeypatch.setattr("os.path.expanduser", lambda path: path)  # neither HOME nor pwd

    assert cache._cache_dir() is None
    assert cache.extract_cache_get("https://example.com/a") is None
    cache.extract_cache_put("https://example.com/a", "content")  # must not raise either


def test_a_put_over_a_corrupt_index_recovers_it(cache_dir):
    """A corrupt index is an empty cache, so the next put simply rebuilds it."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / cache._INDEX_FILENAME).write_text("{not json", encoding="utf-8")
    cache.extract_cache_put("https://example.com/a", "content")
    assert cache.extract_cache_get("https://example.com/a")["content"] == "content"


# ── index housekeeping ────────────────────────────────────────────────────────


def test_index_eviction_keeps_the_newest(monkeypatch, cache_dir):
    monkeypatch.setattr(cache, "_INDEX_MAX_ENTRIES", 3)
    now = time.time()
    cache._save_index({
        f"digest{i}": {"url": f"u{i}", "file": "f", "fetched_at": now + i}
        for i in range(6)
    })
    saved = index_of(cache_dir)
    assert set(saved) == {"digest3", "digest4", "digest5"}


def test_index_eviction_treats_a_broken_entry_as_the_oldest(monkeypatch, cache_dir):
    monkeypatch.setattr(cache, "_INDEX_MAX_ENTRIES", 2)
    now = time.time()
    cache._save_index({
        "broken": "not a mapping",
        "old": {"fetched_at": now - 100},
        "new": {"fetched_at": now},
    })
    assert set(index_of(cache_dir)) == {"old", "new"}


def test_the_index_is_replaced_atomically_leaving_no_temp_files(cache_dir):
    cache.extract_cache_put("https://example.com/a", "content")
    leftovers = [path.name for path in cache_dir.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == []


def test_a_write_failure_is_swallowed(monkeypatch):
    """A full disk costs the next lookup a re-extraction; it never fails the tool call."""
    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(cache.atomic, "write_text", boom)
    cache.extract_cache_put("https://example.com/a", "content")  # must not raise
    assert cache.extract_cache_get("https://example.com/a") is None


# ── shared config with the search memo ────────────────────────────────────────


def test_cache_enabled_false_disables_both_halves(web_home, cache_dir):
    cache.extract_cache_put("https://e.com", "x")
    write_config(web_home, cache_enabled=False)
    assert cache.extract_cache_get("https://e.com") is None

    cache.extract_cache_put("https://fresh.com", "y")
    write_config(web_home)
    assert cache.extract_cache_get("https://fresh.com") is None
    assert not any(path.name.startswith("fresh.com-") for path in cache_dir.iterdir())


@pytest.mark.parametrize("minutes,expected", [
    (0, 60.0),              # floored at one minute
    (99999, 1440 * 60.0),   # ceilinged at 24 hours
    ("bogus", 20 * 60.0),   # the default on garbage
    (5, 300.0),
])
def test_ttl_clamping_is_shared_with_the_search_memo(web_home, minutes, expected):
    write_config(web_home, cache_ttl_minutes=minutes)
    assert cache.ttl_seconds() == expected
