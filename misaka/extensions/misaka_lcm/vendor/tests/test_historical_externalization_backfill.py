"""Operator backfill safety and idempotency tests."""

import importlib.util
import json
from pathlib import Path

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.externalize import get_large_output_storage_dir, maybe_externalize_payload
from hermes_lcm.store import MessageStore


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "backfill_externalized_tool_outputs.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("historical_externalization_backfill", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(tmp_path, *, content="large historical output " * 100, session_id="session-private"):
    home = tmp_path / "hermes"
    database = home / "lcm.db"
    config = LCMConfig(
        database_path=str(database),
        large_output_externalization_enabled=False,
        large_output_externalization_threshold_chars=100,
    )
    store = MessageStore(database, ingest_protection_config=config, hermes_home=str(home))
    store.append(
        session_id,
        {"role": "tool", "tool_call_id": "call-private", "content": content},
    )
    store.close()
    return home, database, config, content


def _run_backfill(module, home, database, config, manifest, *, apply):
    return module.run_backfill(
        database_path=database,
        hermes_home=home,
        manifest_path=manifest,
        threshold_chars=100,
        apply=apply,
        config=config,
    )


def test_dry_run_writes_scrubbed_manifest_without_sidecars_or_db_rewrite(tmp_path):
    module = _load_script()
    home, database, config, content = _seed(tmp_path)
    manifest_path = tmp_path / "dry-run.json"

    result = _run_backfill(module, home, database, config, manifest_path, apply=False)

    assert result["applied"] is False
    assert result["counts"]["eligible"] == 1
    assert result["counts"]["created"] == 0
    assert len(result["items"]) == 1
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "session-private" not in manifest_text
    assert "call-private" not in manifest_text
    assert content not in manifest_text
    storage_dir = get_large_output_storage_dir(config, hermes_home=str(home), create=False)
    assert not storage_dir.exists()
    with module._read_only_connection(database) as connection:
        assert connection.execute("SELECT content FROM messages").fetchone()[0] == content


def test_apply_is_idempotent_and_raw_rows_remain_unchanged(tmp_path):
    module = _load_script()
    home, database, config, content = _seed(tmp_path)

    first = _run_backfill(module, home, database, config, tmp_path / "first.json", apply=True)
    second = _run_backfill(module, home, database, config, tmp_path / "second.json", apply=True)

    assert first["counts"]["created"] == 1
    assert len(first["items"]) == 1
    assert first["items"][0]["created"] is True
    assert second["counts"]["created"] == 0
    assert second["counts"]["existing"] == 1
    assert second["items"] == []
    storage_dir = get_large_output_storage_dir(config, hermes_home=str(home), create=False)
    assert len(list(storage_dir.glob("*.json"))) == 1
    with module._read_only_connection(database) as connection:
        assert connection.execute("SELECT content FROM messages").fetchone()[0] == content


def test_same_path_apply_rerun_preserves_rollback_manifest_ownership(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"

    first = _run_backfill(module, home, database, config, manifest, apply=True)
    second = _run_backfill(module, home, database, config, manifest, apply=True)
    persisted = json.loads(manifest.read_text(encoding="utf-8"))

    assert first["counts"]["created"] == 1
    assert second["counts"]["existing"] == 1
    assert len(persisted["items"]) == 1
    assert persisted["items"][0]["created"] is True

    rollback = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )
    assert rollback["counts"]["deleted"] == 1


def test_same_path_apply_appends_new_owned_sidecars_without_losing_old_ones(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path, content="first historical output " * 100)
    manifest = tmp_path / "apply.json"
    first = _run_backfill(module, home, database, config, manifest, apply=True)
    first_ref = first["items"][0]["ref"]
    store = MessageStore(database, ingest_protection_config=config, hermes_home=str(home))
    store.append(
        "session-private",
        {"role": "tool", "tool_call_id": "call-second", "content": "second historical output " * 100},
    )
    store.close()

    second = _run_backfill(module, home, database, config, manifest, apply=True)

    assert second["counts"]["existing"] == 1
    assert second["counts"]["created"] == 1
    assert len(second["items"]) == 2
    assert first_ref in {item["ref"] for item in second["items"]}


def test_interrupted_manifest_publication_recovers_pending_owned_sidecar(tmp_path, monkeypatch):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "recover.json"
    original_write = module._write_manifest
    writes = 0

    def interrupt_after_sidecar(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("simulated crash before ownership finalization")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(module, "_write_manifest", interrupt_after_sidecar)
    with pytest.raises(OSError, match="simulated crash"):
        _run_backfill(module, home, database, config, manifest, apply=True)

    interrupted = json.loads(manifest.read_text(encoding="utf-8"))
    assert interrupted["state"] == "applying"
    assert len(interrupted["pending_items"]) == 1
    assert interrupted["items"] == []

    monkeypatch.setattr(module, "_write_manifest", original_write)
    recovered = _run_backfill(module, home, database, config, manifest, apply=True)
    assert recovered["state"] == "complete"
    assert recovered["pending_items"] == []
    assert len(recovered["items"]) == 1
    assert recovered["counts"]["existing"] == 1


def test_backfill_refuses_future_database_schema_before_scan_or_sidecar_write(tmp_path, monkeypatch):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "future-schema.json"
    connection = __import__("sqlite3").connect(database)
    connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    with pytest.raises(Exception, match="newer than this build supports"):
        _run_backfill(module, home, database, config, manifest, apply=True)

    assert not manifest.exists()
    storage_dir = get_large_output_storage_dir(config, hermes_home=str(home), create=False)
    assert not storage_dir.exists()


def test_manifest_symlink_is_refused_without_touching_target(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    target = tmp_path / "unrelated-target.json"
    target.write_text("UNRELATED-MUST-SURVIVE", encoding="utf-8")
    manifest = tmp_path / "manifest-link.json"
    manifest.symlink_to(target)

    with pytest.raises(ValueError, match="manifest.*symlink"):
        _run_backfill(module, home, database, config, manifest, apply=False)

    assert manifest.is_symlink()
    assert target.read_text(encoding="utf-8") == "UNRELATED-MUST-SURVIVE"


def test_manifest_publication_does_not_replace_regular_file_that_appears_at_publish_boundary(
    tmp_path, monkeypatch
):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "manifest.json"
    sentinel = "UNRELATED-MANIFEST-MUST-SURVIVE"
    original_link = module.os.link
    injected = False

    def link_with_interloper(source, destination, *args, **kwargs):
        nonlocal injected
        if not injected and destination == manifest.name:
            manifest.write_text(sentinel, encoding="utf-8")
            injected = True
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", link_with_interloper)

    with pytest.raises(FileExistsError, match="manifest path appeared"):
        _run_backfill(module, home, database, config, manifest, apply=False)

    assert injected is True
    assert manifest.read_text(encoding="utf-8") == sentinel


def test_manifest_update_restores_regular_file_swapped_in_at_exchange_boundary(tmp_path, monkeypatch):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "manifest.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    prior_manifest = tmp_path / "prior-manifest.json"
    sentinel = "UNRELATED-MANIFEST-MUST-SURVIVE"
    original_exchange = module._rename_exchange
    injected = False

    def exchange_with_interloper(directory_fd, first, second):
        nonlocal injected
        if not injected and second == manifest.name:
            manifest.replace(prior_manifest)
            manifest.write_text(sentinel, encoding="utf-8")
            injected = True
        return original_exchange(directory_fd, first, second)

    monkeypatch.setattr(module, "_rename_exchange", exchange_with_interloper)

    with pytest.raises(RuntimeError, match="manifest changed during publication"):
        _run_backfill(module, home, database, config, manifest, apply=True)

    assert injected is True
    assert manifest.read_text(encoding="utf-8") == sentinel
    assert json.loads(prior_manifest.read_text(encoding="utf-8"))["state"] == "complete"


def test_same_manifest_cannot_be_reused_for_another_database_or_storage_root(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path / "first")
    other_home, other_database, other_config, _ = _seed(tmp_path / "second")
    manifest = tmp_path / "bound.json"

    first = _run_backfill(module, home, database, config, manifest, apply=True)
    first_ref = first["items"][0]["ref"]

    with pytest.raises(ValueError, match="different database or storage root"):
        _run_backfill(module, other_home, other_database, other_config, manifest, apply=True)

    first_sidecar = get_large_output_storage_dir(config, hermes_home=str(home), create=False) / first_ref
    assert first_sidecar.exists()
    other_storage = get_large_output_storage_dir(other_config, hermes_home=str(other_home), create=False)
    assert not other_storage.exists()


def test_media_shaped_tool_rows_are_skipped(tmp_path):
    module = _load_script()
    media = json.dumps([
        {"type": "input_image", "image_url": "https://example.invalid/image.png"},
        {"type": "text", "text": "x" * 500},
    ])
    home, database, config, _ = _seed(tmp_path, content=media)

    result = _run_backfill(module, home, database, config, tmp_path / "media.json", apply=True)

    assert result["counts"]["skipped_media"] == 1
    assert result["counts"]["created"] == 0


def test_dry_run_counts_already_externalized_rows(tmp_path):
    module = _load_script()
    placeholder = (
        "[Externalized tool output: tool_call_id=call-private-with-a-long-identifier; "
        "chars=1200; bytes=1200; ref=tool-result.json]"
    )
    home, database, config, _ = _seed(tmp_path, content=placeholder)

    result = _run_backfill(module, home, database, config, tmp_path / "externalized.json", apply=False)

    assert result["counts"]["skipped_externalized"] == 1
    assert result["counts"]["eligible"] == 0
    assert result["items"] == []


def test_rollback_dry_run_then_apply_deletes_only_manifest_owned_sidecar(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    ref = json.loads(manifest.read_text(encoding="utf-8"))["items"][0]["ref"]
    sidecar = get_large_output_storage_dir(config, hermes_home=str(home), create=False) / ref

    dry_run = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=False,
        config=config,
    )
    applied = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    assert dry_run["counts"]["eligible"] == 1
    assert dry_run["counts"]["deleted"] == 0
    assert applied["counts"]["deleted"] == 1
    assert applied["counts"]["succeeded"] == 1
    assert applied["counts"]["failed"] == 0
    assert applied["counts"]["skipped"] == 0
    assert applied["failed_paths"] == []
    assert not sidecar.exists()


def test_rollback_rejects_forged_manifest_for_unrelated_sidecar(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    config.large_output_externalization_enabled = True
    victim_content = "unrelated externalized output " * 100
    created = maybe_externalize_payload(
        victim_content,
        kind="tool_result",
        tool_call_id="call-unrelated",
        session_id="session-unrelated",
        role="tool",
        config=config,
        hermes_home=str(home),
        force=True,
    )
    victim = Path(created["path"])
    manifest = tmp_path / "forged.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation": "historical_tool_output_externalization",
                "applied": True,
                "items": [
                    {
                        "ref": victim.name,
                        "sha256": module._sha256(victim_content),
                        "created": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="provenance"):
        module.run_rollback(
            database_path=database,
            hermes_home=home,
            source_manifest_path=manifest,
            apply=True,
            config=config,
        )

    assert victim.exists()


def test_rollback_refuses_foreign_sidecar_even_with_forged_provenance_fields(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    config.large_output_externalization_enabled = True
    victim_content = "unrelated externalized output " * 100
    created = maybe_externalize_payload(
        victim_content,
        kind="tool_result",
        tool_call_id="call-unrelated",
        session_id="session-unrelated",
        role="tool",
        config=config,
        hermes_home=str(home),
        force=True,
    )
    victim = Path(created["path"])
    manifest_id = "a" * 32
    content_sha256 = module._sha256(victim_content)
    ownership_proof = module._ownership_proof(
        manifest_id=manifest_id,
        ref=victim.name,
        content_sha256=content_sha256,
    )
    manifest = tmp_path / "forged-with-provenance.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation": module.BACKFILL_OPERATION,
                "manifest_id": manifest_id,
                "applied": True,
                "state": "complete",
                "target": module._target_binding(
                    database,
                    get_large_output_storage_dir(config, hermes_home=str(home), create=False),
                ),
                "pending_items": [],
                "items": [
                    {
                        "ref": victim.name,
                        "sha256": content_sha256,
                        "created": True,
                        "ownership_proof": ownership_proof,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    assert result["counts"]["eligible"] == 0
    assert result["counts"]["deleted"] == 0
    assert result["counts"]["skipped_provenance_mismatch"] == 1
    assert victim.exists()


def test_rollback_skips_invalid_ref(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    source = json.loads(manifest.read_text(encoding="utf-8"))
    source["items"][0]["ref"] = "../outside.json"
    manifest.write_text(json.dumps(source), encoding="utf-8")

    result = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    assert result["counts"]["skipped_invalid_ref"] == 1
    assert result["counts"]["skipped"] == 1


def test_rollback_skips_missing_sidecar(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    ref = json.loads(manifest.read_text(encoding="utf-8"))["items"][0]["ref"]
    sidecar = get_large_output_storage_dir(config, hermes_home=str(home), create=False) / ref
    sidecar.unlink()

    result = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    assert result["counts"]["skipped_missing"] == 1
    assert result["counts"]["skipped"] == 1


def test_rollback_skips_symlink(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    ref = json.loads(manifest.read_text(encoding="utf-8"))["items"][0]["ref"]
    sidecar = get_large_output_storage_dir(config, hermes_home=str(home), create=False) / ref
    target = tmp_path / "sidecar-target.json"
    sidecar.replace(target)
    sidecar.symlink_to(target)

    result = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    assert result["counts"]["skipped_symlink"] == 1
    assert result["counts"]["skipped"] == 1
    assert sidecar.is_symlink()


def test_rollback_rejects_non_schema_v1_manifest(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "invalid-schema.json"
    manifest.write_text(json.dumps({"schema_version": 2, "applied": True}), encoding="utf-8")

    with pytest.raises(ValueError, match="applied schema-v1"):
        module.run_rollback(
            database_path=database,
            hermes_home=home,
            source_manifest_path=manifest,
            apply=True,
            config=config,
        )


def test_rollback_rejects_non_applied_source_manifest(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "dry-run.json"
    _run_backfill(module, home, database, config, manifest, apply=False)

    with pytest.raises(ValueError, match="applied schema-v1"):
        module.run_rollback(
            database_path=database,
            hermes_home=home,
            source_manifest_path=manifest,
            apply=True,
            config=config,
        )


def test_rollback_rejects_symlink_manifest_without_reading_target(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    original = manifest.read_bytes()
    target = tmp_path / "manifest-target.json"
    manifest.replace(target)
    manifest.symlink_to(target)

    with pytest.raises(ValueError, match="manifest.*symlink"):
        module.run_rollback(
            database_path=database,
            hermes_home=home,
            source_manifest_path=manifest,
            apply=True,
            config=config,
        )

    assert manifest.is_symlink()
    assert target.read_bytes() == original


def test_rollback_rejects_manifest_with_wrong_operation(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    source = json.loads(manifest.read_text(encoding="utf-8"))
    ref = source["items"][0]["ref"]
    source["operation"] = "foreign_operation"
    manifest.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ValueError, match="requires operation"):
        module.run_rollback(
            database_path=database,
            hermes_home=home,
            source_manifest_path=manifest,
            apply=True,
            config=config,
        )

    sidecar = get_large_output_storage_dir(config, hermes_home=str(home), create=False) / ref
    assert sidecar.exists()


def test_rollback_reports_partial_failure_and_continues(tmp_path, monkeypatch, capsys):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path, content="first historical output " * 100)
    store = MessageStore(database, ingest_protection_config=config, hermes_home=str(home))
    store.append(
        "session-private",
        {"role": "tool", "tool_call_id": "call-second", "content": "second historical output " * 100},
    )
    store.close()
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    refs = [item["ref"] for item in json.loads(manifest.read_text(encoding="utf-8"))["items"]]
    storage_dir = get_large_output_storage_dir(config, hermes_home=str(home), create=False)
    failed_path = storage_dir / refs[0]
    succeeded_path = storage_dir / refs[1]
    original_unlink = module._quarantine_unlink

    def fail_one_unlink(directory_fd, ref, identity):
        if ref == failed_path.name:
            raise OSError("simulated unlink failure")
        return original_unlink(directory_fd, ref, identity)

    monkeypatch.setattr(module, "_quarantine_unlink", fail_one_unlink)

    exit_code = module.main(
        [
            "--database",
            str(database),
            "--hermes-home",
            str(home),
            "--rollback",
            str(manifest),
            "--apply",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert result["counts"]["eligible"] == 2
    assert result["counts"]["succeeded"] == 1
    assert result["counts"]["failed"] == 1
    assert result["counts"]["skipped"] == 0
    assert result["failed_paths"] == [str(failed_path)]
    assert failed_path.exists()
    assert not succeeded_path.exists()


def test_rollback_does_not_delete_regular_file_swapped_after_validation(tmp_path, monkeypatch):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    result = _run_backfill(module, home, database, config, manifest, apply=True)
    ref = result["items"][0]["ref"]
    storage_dir = get_large_output_storage_dir(config, hermes_home=str(home), create=False)
    sidecar = storage_dir / ref
    checked_sidecar = storage_dir / "checked-sidecar-backup.json"
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text("UNRELATED-MUST-SURVIVE", encoding="utf-8")
    original_unlink = module._quarantine_unlink

    def swap_before_delete(directory_fd, candidate_ref, identity):
        sidecar.replace(checked_sidecar)
        unrelated.replace(sidecar)
        return original_unlink(directory_fd, candidate_ref, identity)

    monkeypatch.setattr(module, "_quarantine_unlink", swap_before_delete)
    rollback = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    assert rollback["counts"]["deleted"] == 0
    assert rollback["counts"]["failed"] == 1
    assert sidecar.read_text(encoding="utf-8") == "UNRELATED-MUST-SURVIVE"
    assert checked_sidecar.exists()


def test_rollback_fails_closed_when_quarantine_is_replaced_after_identity_check(tmp_path, monkeypatch):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    storage_dir = get_large_output_storage_dir(config, hermes_home=str(home), create=False)
    checked_sidecar = tmp_path / "checked-sidecar-backup.json"
    replacement = tmp_path / "unrelated-replacement.json"
    replacement.write_text("UNRELATED-MUST-SURVIVE", encoding="utf-8")
    original_unlink = module._unlink_verified_quarantine
    swapped = False

    def swap_after_identity_check(directory_fd, quarantine, identity):
        nonlocal swapped
        quarantine_path = storage_dir / quarantine
        quarantine_path.replace(checked_sidecar)
        replacement.replace(quarantine_path)
        swapped = True
        return original_unlink(directory_fd, quarantine, identity)

    monkeypatch.setattr(module, "_unlink_verified_quarantine", swap_after_identity_check)
    rollback = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    assert swapped is True
    assert rollback["counts"]["deleted"] == 0
    assert rollback["counts"]["failed"] == 1
    assert checked_sidecar.exists()
    quarantined = list(storage_dir.glob(".rollback-*.tmp"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "UNRELATED-MUST-SURVIVE"


def test_rollback_refuses_storage_directory_writable_by_other_users(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    result = _run_backfill(module, home, database, config, manifest, apply=True)
    ref = result["items"][0]["ref"]
    storage_dir = get_large_output_storage_dir(config, hermes_home=str(home), create=False)
    sidecar = storage_dir / ref
    storage_dir.chmod(0o777)

    with pytest.raises(PermissionError, match="must not be writable by group or other users"):
        module.run_rollback(
            database_path=database,
            hermes_home=home,
            source_manifest_path=manifest,
            apply=True,
            config=config,
        )

    assert sidecar.exists()


def test_backfill_apply_write_failure_returns_nonzero_with_failed_path(tmp_path, monkeypatch, capsys):
    module = _load_script()
    home, database, _config, _ = _seed(tmp_path)
    manifest = tmp_path / "failed-apply.json"
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    def fail_write(*_args, **_kwargs):
        raise OSError("injected sidecar write failure")

    monkeypatch.setattr(module, "_replace_externalized_payload", fail_write)
    exit_code = module.main(
        [
            "--database",
            str(database),
            "--hermes-home",
            str(home),
            "--manifest",
            str(manifest),
            "--threshold-chars",
            "100",
            "--apply",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert result["counts"]["failed"] == 1
    assert result["counts"]["created"] == 0
    assert len(result["failed_paths"]) == 1
    assert unrelated.read_text(encoding="utf-8") == "keep me"


def test_rollback_refuses_referenced_sidecar(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    ref = json.loads(manifest.read_text(encoding="utf-8"))["items"][0]["ref"]
    connection = __import__("sqlite3").connect(database)
    connection.execute("UPDATE messages SET content = ?", (f"[Externalized tool output: ref={ref}]",))
    connection.commit()
    connection.close()

    result = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    assert result["counts"]["skipped_referenced"] == 1
    sidecar = get_large_output_storage_dir(config, hermes_home=str(home), create=False) / ref
    assert sidecar.exists()


def test_rollback_refuses_sidecar_referenced_in_nested_tool_calls(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    ref = json.loads(manifest.read_text(encoding="utf-8"))["items"][0]["ref"]
    connection = __import__("sqlite3").connect(database)
    connection.execute(
        "UPDATE messages SET tool_calls = ?",
        (json.dumps([{"nested": {"payload": f"[Externalized tool output: ref={ref}]"}}]),),
    )
    connection.commit()
    connection.close()

    result = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    sidecar = get_large_output_storage_dir(config, hermes_home=str(home), create=False) / ref
    assert result["counts"]["skipped_referenced"] == 1
    assert result["counts"]["deleted"] == 0
    assert sidecar.exists()


def test_rollback_refuses_digest_mismatch(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)
    ref = json.loads(manifest.read_text(encoding="utf-8"))["items"][0]["ref"]
    sidecar = get_large_output_storage_dir(config, hermes_home=str(home), create=False) / ref
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["content"] = "changed"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    result = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=config,
    )

    assert result["counts"]["skipped_digest_mismatch"] == 1
    assert sidecar.exists()


def _redacting_config(database, patterns):
    return LCMConfig(
        database_path=str(database),
        large_output_externalization_enabled=False,
        large_output_externalization_threshold_chars=100,
        sensitive_patterns_enabled=True,
        sensitive_patterns=list(patterns),
    )


def test_apply_redacts_sensitive_content_before_writing_sidecar(tmp_path):
    module = _load_script()
    secret = "SUPERSECRETVALUE1234"
    content = f"api_key={secret}\n" + ("historical tool output line\n" * 40)
    home, database, _, _ = _seed(tmp_path, content=content)
    redacting_config = _redacting_config(database, ["api_key"])
    manifest = tmp_path / "apply.json"

    result = _run_backfill(module, home, database, redacting_config, manifest, apply=True)

    assert result["counts"]["created"] == 1
    storage_dir = get_large_output_storage_dir(redacting_config, hermes_home=str(home), create=False)
    sidecars = list(storage_dir.glob("*.json"))
    assert len(sidecars) == 1
    payload = json.loads(sidecars[0].read_text(encoding="utf-8"))
    # The current sensitive-pattern policy is applied before persisting, exactly as
    # live ingest does, so the raw secret never reaches the new retention surface.
    assert secret not in json.dumps(payload)
    assert "LCM sensitive redaction" in payload["content"]
    # The manifest digest and ownership proof bind the redacted content actually stored.
    persisted = json.loads(manifest.read_text(encoding="utf-8"))
    assert module._sha256(payload["content"]) == persisted["items"][0]["sha256"]
    assert persisted["redaction"] == {"enabled": True, "active_patterns": ["api_key"]}
    # Raw message rows are never rewritten.
    with module._read_only_connection(database) as connection:
        assert secret in connection.execute("SELECT content FROM messages").fetchone()[0]
    # Rollback still recognizes and deletes its own redacted sidecar.
    rollback = module.run_rollback(
        database_path=database,
        hermes_home=home,
        source_manifest_path=manifest,
        apply=True,
        config=redacting_config,
    )
    assert rollback["counts"]["deleted"] == 1


def test_manifest_never_leaks_tool_call_or_session_ids(tmp_path):
    module = _load_script()
    home, database, config, _ = _seed(
        tmp_path, content="historical output " * 100, session_id="session-supersecret-id"
    )
    manifest = tmp_path / "apply.json"
    _run_backfill(module, home, database, config, manifest, apply=True)

    manifest_text = manifest.read_text(encoding="utf-8")
    # The operator guide promises the manifest omits raw tool-call and session ids;
    # refs also carry no tool-call stub, only the historical-backfill marker.
    assert "session-supersecret-id" not in manifest_text
    assert "call-private" not in manifest_text
    persisted = json.loads(manifest_text)
    for record in persisted["items"] + persisted.get("pending_items", []):
        assert "tool_call_id" not in record
        assert "session_id" not in record
        assert "historical-backfill" in str(record.get("ref", ""))


def test_backfill_refuses_manifest_reuse_under_changed_redaction_policy(tmp_path):
    module = _load_script()
    home, database, _, _ = _seed(tmp_path)
    manifest = tmp_path / "apply.json"

    first = _run_backfill(module, home, database, _redacting_config(database, ["api_key"]), manifest, apply=True)
    assert first["counts"]["created"] == 1

    with pytest.raises(ValueError, match="different sensitive-pattern redaction policy"):
        _run_backfill(module, home, database, _redacting_config(database, ["bearer_token"]), manifest, apply=True)
