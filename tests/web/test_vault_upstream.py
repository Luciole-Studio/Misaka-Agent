# Selected Hermes 62e5f46 store/classifier/handler regressions (MIT).
# Host-specific supervisor, gateway and backup tests have native counterparts.
"""Tests for the vault-backed password-blind browser autofill feature.

Covers:
- VaultStore: encrypt/decrypt round-trip, file perms, identifier-as-metadata
  (login secret payload is password-only)
- login-control classifier: scoring + new-password/one-time-code exclusion,
  password-only fill selection
- origin-binding refusal (pre-check + in-script TOCTOU assert)
- fail-closed secret eval (no argv fallback)
- vault-value redaction registry (browser_cdp read-back regression)
- tool gating: check_fn False when the vault is empty
"""

from __future__ import annotations

import json
import os
import re
import stat
from unittest.mock import patch

import pytest

from misaka.core.web.browser.vault.classifier import (
    ClassifiedLoginControl,
    LoginControl,
    build_fill_js,
    classify_login_control,
    select_password_fill,
)
from misaka.core.web.browser.vault.store import (
    VaultError,
    VaultStore,
    normalize_origin,
    scrub_secret_from_text,
)


@pytest.fixture()
def store(tmp_path):
    return VaultStore(base_dir=tmp_path / "vault")


def _add_login(store, origin="https://example.com", password="s3cret-pw"):
    return store.add_item(
        kind="login",
        label="Example login",
        origin=origin,
        secret={
            "identifier_type": "email",
            "identifier": "user@example.com",
            "password": password,
            "origin": origin,
        },
    )


# ---------------------------------------------------------------------------
# VaultStore
# ---------------------------------------------------------------------------

class TestVaultStore:
    def test_roundtrip_encrypt_decrypt(self, store):
        meta = _add_login(store)
        secret = store.resolve_secret(meta.id)
        # Design: login secret payload is password-only; identifier is metadata.
        assert secret == {"password": "s3cret-pw"}
        assert meta.identifier == "user@example.com"
        assert meta.identifier_type == "email"

    def test_vault_file_never_contains_password(self, store, tmp_path):
        _add_login(store)
        blob = (tmp_path / "vault" / "vault.json.enc").read_bytes()
        assert b"s3cret-pw" not in blob

    def test_file_permissions_0600(self, store, tmp_path):
        _add_login(store)
        for name in ("vault.json.enc", "vault.key"):
            mode = stat.S_IMODE(os.stat(tmp_path / "vault" / name).st_mode)
            assert mode == 0o600, f"{name} has mode {oct(mode)}"

    def test_listing_is_password_free(self, store):
        meta = _add_login(store)
        items = store.list_items()
        assert len(items) == 1
        dumped = json.dumps(items[0].to_dict())
        assert "s3cret-pw" not in dumped
        assert "password" not in dumped
        # Identifier IS visible metadata now.
        assert items[0].identifier == "user@example.com"
        assert items[0].identifier_type == "email"
        assert items[0].id == meta.id
        assert items[0].origin == "https://example.com"

    def test_remove_item(self, store):
        meta = _add_login(store)
        assert store.remove_item(meta.id) is True
        assert store.remove_item(meta.id) is False
        assert store.list_items() == []

    def test_login_requires_origin(self, store):
        with pytest.raises(VaultError):
            store.add_item(
                kind="login",
                label="x",
                secret={
                    "identifier_type": "email",
                    "identifier": "a@b.c",
                    "password": "p",
                },
            )

    def test_all_kinds_supported(self, store):
        store.add_item(kind="payment", label="Card", origin="https://shop.test", secret=_CARD)
        store.add_item(kind="address", label="Home", secret=_ADDRESS)
        kinds = {m.kind for m in store.list_items()}
        assert kinds == {"payment", "address"}

    def test_checkout_kinds_keep_only_canonical_fields(self, store):
        """The fill maps canonical names → autocomplete tokens; a stray ad-hoc key would be stored (secret!)
        yet unfillable, and a missing required field would make the item dead on every checkout."""
        meta = store.add_item(kind="payment", label="Card", secret={**_CARD, "note": "personal"})
        assert "note" not in store.resolve_secret(meta.id)
        with pytest.raises(VaultError, match="cvc"):
            store.add_item(kind="payment", label="Card", secret={k: v for k, v in _CARD.items() if k != "cvc"})

    def test_unknown_kind_rejected(self, store):
        with pytest.raises(VaultError):
            store.add_item(kind="totp", label="x", secret={})

    def test_has_items(self, store):
        assert store.has_items() is False
        _add_login(store)
        assert store.has_items() is True

    def test_normalize_origin(self):
        assert normalize_origin("https://Example.com:443/login?x=1") == "https://example.com"
        assert normalize_origin("http://localhost:8931/") == "http://localhost:8931"
        assert normalize_origin("http://site.test:80") == "http://site.test"
        with pytest.raises(VaultError):
            normalize_origin("example.com")

    def test_scrub_secret_from_text(self):
        secret = {"password": "hunter22x", "identifier": "me@x.io"}
        out = scrub_secret_from_text("boom hunter22x at me@x.io", secret)
        assert "hunter22x" not in out
        assert "me@x.io" not in out


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

_CARD = {"card_number": "4111111111111111", "cardholder_name": "A User", "exp_month": "7", "exp_year": "2029",
         "cvc": "123", "billing_postal_code": "94110"}
_ADDRESS = {"address_line1": "1 Main St", "city": "Springfield", "postal_code": "12345", "country": "US"}


def _ctrl(**kw):
    base = {"autocomplete": "", "form_index": 0, "index": 0, "label": "", "name": "", "type": "text"}
    base.update(kw)
    return LoginControl(**base)


class TestClassifier:
    def test_autocomplete_exact_match_scores_100(self):
        for token in ("username", "email", "tel", "current-password"):
            res = classify_login_control(_ctrl(autocomplete=token))
            assert res is not None and res.score == 100 and res.token == token

    def test_new_password_autocomplete_excluded(self):
        assert classify_login_control(
            _ctrl(autocomplete="new-password", type="password")
        ) is None

    def test_one_time_code_excluded(self):
        assert classify_login_control(_ctrl(autocomplete="one-time-code")) is None

    def test_label_new_password_excluded(self):
        for label in ("New password", "Confirm Password", "create-password", "Repeat  password"):
            assert classify_login_control(_ctrl(type="password", label=label)) is None, label

    def test_password_type_scores_90(self):
        res = classify_login_control(_ctrl(type="password"))
        assert res.score == 90 and res.token == "current-password"

    def test_email_tel_types_score_85(self):
        assert classify_login_control(_ctrl(type="email")).score == 85
        res = classify_login_control(_ctrl(type="tel"))
        assert res.score == 85 and res.token == "tel"

    def test_label_heuristics(self):
        assert classify_login_control(_ctrl(label="E-mail address")).token == "email"
        assert classify_login_control(_ctrl(name="mobile_number")).token == "tel"
        res = classify_login_control(_ctrl(label="Username or account"))
        assert res.token == "username" and res.score == 70

    def test_unmatched_returns_none(self):
        assert classify_login_control(_ctrl(label="Search the docs")) is None

    def test_select_password_fill_picks_best_password(self):
        user = ClassifiedLoginControl(_ctrl(index=0, form_index=0, autocomplete="username"), 100, "username")
        pw_heur = ClassifiedLoginControl(_ctrl(index=1, form_index=0, type="password"), 90, "current-password")
        pw_exact = ClassifiedLoginControl(_ctrl(index=3, form_index=0, autocomplete="current-password"), 100, "current-password")
        fills = select_password_fill([user, pw_heur, pw_exact], "p")
        # Password only — the identifier field is never filled by the vault.
        assert [(f["index"], f["token"]) for f in fills] == [(3, "current-password")]

    def test_select_password_fill_requires_password_field(self):
        user = ClassifiedLoginControl(_ctrl(index=0, autocomplete="username"), 100, "username")
        assert select_password_fill([user], "p") == []

    def test_select_password_fill_single_field_only(self):
        pw1 = ClassifiedLoginControl(_ctrl(index=1, type="password"), 90, "current-password")
        pw2 = ClassifiedLoginControl(_ctrl(index=2, type="password"), 90, "current-password")
        fills = select_password_fill([pw1, pw2], "p")
        assert len(fills) == 1 and fills[0]["index"] == 1

    def test_build_fill_js_contains_events(self):
        js = build_fill_js(
            [{"index": 0, "token": "current-password", "value": "x"}],
            expected_origin="https://example.com",
        )
        assert "InputEvent" in js and '"change"' in js and "filled" in js

    def test_build_fill_js_leaves_no_dom_marker_and_binds_target_to_inspection(self):
        # P1-1: no persistent selector for filled controls. The fill targets the input by the
        # <nonce>:<index> stamp ITS OWN inspection wrote (a bare index is re-resolved by position,
        # and a second inspection in between would re-stamp — either way the password could land in
        # another field); stamps carry no secret and the fill script strips every one before returning.
        js = build_fill_js(
            [{"index": 0, "token": "current-password", "value": "x"}],
            expected_origin="https://example.com",
        )
        assert "vaultSecret" not in js
        assert "data-vault-secret" not in js
        assert "elements[f.index]" not in js
        assert "[data-hermes-vault-slot=" in js and "nonce + ':' + f.index" in js
        assert 'f.token === "current-password" && el.type !== "password"' in js  # a password fill never lands in a text box
        assert js.index('removeAttribute("data-hermes-vault-slot")') > js.index("setter.set.call")

    def test_build_fill_js_asserts_origin_before_any_write(self):
        # P1-2: the origin assert must run inside the SAME script, before
        # any element write.
        js = build_fill_js(
            [{"index": 0, "token": "current-password", "value": "x"}],
            expected_origin="https://example.com",
        )
        assert '"https://example.com"' in js
        assert "window.location.origin" in js
        assert "origin_changed" in js
        assert js.index("origin_changed") < js.index("querySelectorAll")


# ---------------------------------------------------------------------------
# Browser tool: origin binding + gating
# ---------------------------------------------------------------------------









class TestSaveLoginPrompt:
    """browser_vault_save_login: the surface prompt supplies the login, the tool stores it bound to the page
    origin and fills. The password must never come back in the tool result."""

    def test_saves_to_page_origin_and_never_echoes_the_password(self, store, monkeypatch):
        from misaka.core.web.browser.vault import tools as browser_vault_tool
        from misaka.core.web.browser.vault.backends import unlock as unlock_mod

        seen = {}

        def prompt(origin, site):
            seen["origin"], seen["site"] = origin, site
            return {"identifier": "tek@acme.test", "password": "hunter2-very-secret"}

        unlock_mod.set_save_login_prompt_callback(prompt)
        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task_id: "https://acme.test")
        monkeypatch.setattr(browser_vault_tool, "browser_vault_fill",
                            lambda handle, task_id=None: json.dumps({"success": True, "filled_fields": 1}))
        with patch("misaka.core.web.browser.vault.store.get_vault_store", return_value=store), \
             patch("misaka.core.web.browser.vault.backends.unlock.can_prompt_here", return_value=True):
            out = json.loads(browser_vault_tool.browser_vault_save_login(task_id="t1"))
        unlock_mod.set_save_login_prompt_callback(None)

        assert out["success"] is True and out["identifier"] == "tek@acme.test"
        assert "hunter2" not in json.dumps(out)
        assert seen == {"origin": "https://acme.test", "site": "acme.test"}
        [meta] = store.list_items()
        assert meta.origin == "https://acme.test" and meta.identifier == "tek@acme.test"

    def test_declined_or_headless_stores_nothing(self, store, monkeypatch):
        from misaka.core.web.browser.vault import tools as browser_vault_tool
        from misaka.core.web.browser.vault.backends import unlock as unlock_mod

        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task_id: "https://acme.test")
        with patch("misaka.core.web.browser.vault.store.get_vault_store", return_value=store):
            unlock_mod.set_save_login_prompt_callback(lambda origin, site: None)
            with patch("misaka.core.web.browser.vault.backends.unlock.can_prompt_here", return_value=True):
                declined = json.loads(browser_vault_tool.browser_vault_save_login())
            with patch("misaka.core.web.browser.vault.backends.unlock.can_prompt_here", return_value=False):
                headless = json.loads(browser_vault_tool.browser_vault_save_login())
            unlock_mod.set_save_login_prompt_callback(None)
        assert declined["error_type"] == "save_declined"
        assert headless["error_type"] == "prompt_unavailable"
        assert store.list_items() == []


class TestManagerAutoDetection:
    def test_installed_manager_is_a_source_without_config_and_config_can_opt_out(self):
        from misaka.core.web.browser.vault.backends import base

        with patch.object(base, "is_installed", return_value=True):
            with patch.object(base, "_cfg", return_value={}):
                assert {b.name for b in base.enabled_backends()} == {"local", "onepassword", "bitwarden"}
            with patch.object(base, "_cfg", return_value={"bitwarden": {"enabled": False}}):
                assert {b.name for b in base.enabled_backends()} == {"local", "onepassword"}
        with patch.object(base, "is_installed", return_value=False), patch.object(base, "_cfg", return_value={}):
            assert [b.name for b in base.enabled_backends()] == ["local"]




class TestTwoFactor:
    def test_totp_matches_rfc6238_vector_and_seed_normalisation(self):
        from misaka.core.web.browser.vault.store import (
            VaultError,
            normalize_otp_secret,
            totp_now,
        )

        seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
        assert totp_now(seed, digits=8, at=59) == "94287082"
        assert totp_now(seed, at=1111111109) == "081804"
        assert normalize_otp_secret("otpauth://totp/GitHub:tek?secret=jbsw y3dp ehpk3pxp&issuer=GitHub") == "JBSWY3DPEHPK3PXP"
        with pytest.raises(VaultError):
            normalize_otp_secret("not base32!")
        # Non-default otpauth parameters are kept and honoured (RFC 6238 SHA-256 / 8-digit vector at T=59).
        stored = normalize_otp_secret(f"otpauth://totp/x?secret={'GEZDGNBVGY3TQOJQ' * 2}&digits=8&period=30&algorithm=SHA256")
        assert stored.endswith("|8|30|SHA256")
        sha256_seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZA"  # "1234567890" * 3.2 -> RFC 32-byte seed
        assert totp_now(sha256_seed + "|8|30|SHA256", at=59) == "46119246"
        assert totp_now("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ|6|60|SHA1", at=119) == totp_now("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", period=60, at=119)
        with pytest.raises(VaultError):
            normalize_otp_secret("otpauth://hotp/x?secret=JBSWY3DPEHPK3PXP&counter=1")

    def test_saved_authenticator_key_mints_codes_without_asking(self, store, monkeypatch):
        """The whole point: with a seed on the login, enter_code never prompts and the code never comes back."""
        from misaka.core.web.browser.vault import tools as browser_vault_tool
        from misaka.core.web.browser.vault.backends import unlock as unlock_mod

        meta = store.add_item("login", "gh", {"identifier_type": "username", "identifier": "tek", "password": "pw",
                                              "otp_secret": "JBSWY3DPEHPK3PXP"}, origin="https://github.com")
        assert store.get_meta(meta.id).has_otp is True
        asked = []
        unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "000000")
        controls = [{"index": 0, "type": "text", "name": "otp", "label": "Authentication code", "autocomplete": "one-time-code"}]
        seen = {}

        def fake_eval(task_id, expr):
            return {"success": True, "result": json.dumps(controls) if "querySelectorAll" in expr else "https://github.com/sessions/two-factor"}

        def fake_secret(task_id, expr):
            seen["expr"] = expr
            return {"success": True, "result": json.dumps({"filled": 1})}

        with patch("misaka.core.web.browser.vault.store.get_vault_store", return_value=store), \
             patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_secret):
            raw = browser_vault_tool.browser_vault_enter_code(meta.id, task_id="t")
        unlock_mod.set_code_prompt_callback(None)
        out = json.loads(raw)
        assert out["success"] and out["source"] == "local" and asked == []
        code = re.search(r'"value": "(\d{6})"', seen["expr"]).group(1)
        assert code not in raw  # the code went to the page, not to the model

    def test_without_a_key_the_user_is_asked_and_split_boxes_get_one_digit_each(self, store):
        from misaka.core.web.browser.vault import tools as browser_vault_tool
        from misaka.core.web.browser.vault.backends import unlock as unlock_mod

        unlock_mod.set_code_prompt_callback(lambda site, hint: "246 810")
        boxes = [{"index": i, "type": "tel", "name": f"digit{i}", "label": "", "autocomplete": "one-time-code",
                  "formIndex": 0, "maxLength": 1} for i in range(6)]
        seen = {}
        fake_eval = lambda t, e: {"success": True, "result": json.dumps(boxes) if "querySelectorAll" in e else "https://acme.test/2fa"}

        def fake_secret(t, e):
            seen["expr"] = e
            return {"success": True, "result": json.dumps({"filled": 6})}

        with patch("misaka.core.web.browser.vault.backends.unlock.can_prompt_here", return_value=True), \
             patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_secret):
            out = json.loads(browser_vault_tool.browser_vault_enter_code(task_id="t"))
            unlock_mod.set_code_prompt_callback(lambda site, hint: "")
            declined = json.loads(browser_vault_tool.browser_vault_enter_code(task_id="t"))
        unlock_mod.set_code_prompt_callback(None)
        assert out["success"] and out["source"] == "user" and out["filled_fields"] == 6
        assert re.findall(r'"value": "(\d)"', seen["expr"]) == list("246810")
        assert declined["error_type"] == "code_declined"

    def test_several_code_like_inputs_that_are_not_a_digit_widget_get_one_field(self):
        """Reviewer case: a page with 4+ code-ish inputs (promo code, zip code, a real OTP box...) must never
        get a digit sprayed across them. Only an unmistakable maxlength=1 same-form adjacent group splits."""
        from misaka.core.web.browser.vault.classifier import (
            ClassifiedLoginControl,
            LoginControl,
            build_otp_fills,
        )

        def ctl(i, form=0, maxlen=None, score=70):
            return ClassifiedLoginControl(LoginControl("", form, i, "", f"code{i}", "text", maxlen), score, "one-time-code")

        scattered = [ctl(0), ctl(3), ctl(7), ctl(9, form=1), ctl(12, score=100)]
        assert build_otp_fills(scattered, "246810") == [{"index": 12, "token": "one-time-code", "value": "246810"}]
        # maxlength=1 but different forms / non-adjacent: still one field
        assert len(build_otp_fills([ctl(i, form=i % 2, maxlen=1) for i in range(6)], "246810")) == 1
        assert len(build_otp_fills([ctl(i * 2, maxlen=1) for i in range(6)], "246810")) == 1
        # five boxes for a six-digit code: one field
        assert len(build_otp_fills([ctl(i, maxlen=1) for i in range(5)], "246810")) == 1
        # the real widget
        assert [f["value"] for f in build_otp_fills([ctl(i + 4, maxlen=1) for i in range(6)], "246810")] == list("246810")

    def test_no_code_field_points_at_passkey_or_device_approval(self):
        from misaka.core.web.browser.vault import tools as browser_vault_tool

        fake_eval = lambda t, e: {"success": True, "result": json.dumps([{"index": 0, "type": "text", "name": "q", "label": "Search", "autocomplete": ""}]) if "querySelectorAll" in e else "https://acme.test/approve"}
        with patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval):
            out = json.loads(browser_vault_tool.browser_vault_enter_code(task_id="t"))
        assert out["error_type"] == "no_code_field" and "device" in out["error"]
