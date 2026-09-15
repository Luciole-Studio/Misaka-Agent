"""Regressions from the independent Office acceptance audit."""
import hashlib
import os
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from docx import Document

from misaka.core.documents.office import docx as reader
from misaka.core.tools import _office

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'


def package(tmp_path, body, extra=None):
    path = tmp_path / 'fixture.docx'
    Document().save(path)
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    document = ET.fromstring(parts['word/document.xml'])
    container = document.find(f'{{{W}}}body')
    container.insert(0, ET.fromstring(f'<w:p xmlns:w="{W}" xmlns:r="{R}">{body}</w:p>'))
    parts['word/document.xml'] = ET.tostring(document)
    parts.update(extra or {})
    with zipfile.ZipFile(path, 'w') as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return path


def test_backup_cleanup_error_does_not_claim_rollback(tmp_path, monkeypatch):
    path = tmp_path / 'original.txt'
    path.write_text('before\n')
    real_unlink = os.unlink
    def deny_backup_delete(name, *args, **kwargs):
        if Path(name).name.startswith('.office-backup-'):
            raise PermissionError('injected backup cleanup failure')
        return real_unlink(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(_office.os, 'unlink', deny_backup_delete)
        receipt = _office.run_ops(path, [{'append': {'content': 'after\n'}}])
    assert receipt.startswith('✓'), receipt
    assert 'file unchanged' not in receipt
    assert 'cleanup failed' in receipt
    assert path.read_text() == 'before\nafter\n'
    backup, = tmp_path.glob('.office-backup-*')
    assert str(backup) in receipt
    assert backup.read_text() == 'before\n'


@pytest.mark.parametrize('kind', ['footnote', 'endnote'])
@pytest.mark.parametrize('has_note_rels', [True, False])
def test_note_hyperlink_uses_its_own_part_relationships(tmp_path, kind, has_note_rels):
    body = f'<w:r><w:t>Body</w:t><w:{kind}Reference w:id="2"/></w:r>'
    path = package(tmp_path, body)
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    rels = ET.fromstring(parts['word/_rels/document.xml.rels'])
    ET.SubElement(rels, f'{{{PKG}}}Relationship', {'Id':'rIdCollision', 'Type':R+'/hyperlink', 'Target':'https://body.example/wrong', 'TargetMode':'External'})
    ET.SubElement(rels, f'{{{PKG}}}Relationship', {'Id':'rIdNotes', 'Type':R+'/'+kind+'s', 'Target':kind+'s.xml'})
    parts['word/_rels/document.xml.rels'] = ET.tostring(rels)
    parts[f'word/{kind}s.xml'] = f'<w:{kind}s xmlns:w="{W}" xmlns:r="{R}"><w:{kind} w:id="2"><w:p><w:hyperlink r:id="rIdCollision"><w:r><w:t>Actual source</w:t></w:r></w:hyperlink></w:p></w:{kind}></w:{kind}s>'.encode()
    parts[f'word/_rels/{kind}s.xml.rels'] = f'<Relationships xmlns="{PKG}"><Relationship Id="rIdCollision" Type="{R}/hyperlink" Target="https://notes.example/correct" TargetMode="External"/></Relationships>'.encode()
    if not has_note_rels:
        del parts[f'word/_rels/{kind}s.xml.rels']
    ct = ET.fromstring(parts['[Content_Types].xml'])
    ET.SubElement(ct, f'{{{CT}}}Override', {'PartName':f'/word/{kind}s.xml','ContentType':f'application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}s+xml'})
    parts['[Content_Types].xml'] = ET.tostring(ct)
    with zipfile.ZipFile(path, 'w') as z:
        for name, data in parts.items(): z.writestr(name, data)
    actual = reader.render(path)
    assert ('<https://notes.example/correct>' in actual) is has_note_rels
    assert 'Actual source' in actual
    assert '<https://body.example/wrong>' not in actual


def test_simple_field_keeps_existing_display_text(tmp_path):
    path = package(tmp_path, '<w:r><w:t>Statement date: </w:t></w:r><w:fldSimple w:instr="DATE"><w:r><w:t>2026-09-15</w:t></w:r></w:fldSimple>')
    actual = reader.render(path)
    assert '2026-09-15' in actual


@pytest.mark.parametrize('fail_operation', [False, True])
def test_staging_cleanup_preserves_operation_outcome(tmp_path, monkeypatch, fail_operation):
    path = tmp_path / 'original.txt'
    path.write_text('before\n')
    real_rmtree = _office.shutil.rmtree

    def deny_staging_delete(name, *args, **kwargs):
        if Path(name).name.startswith('.office-'):
            raise PermissionError('injected staging cleanup failure')
        return real_rmtree(name, *args, **kwargs)

    op = {'unknown': {}} if fail_operation else {'append': {'content': 'after\n'}}
    with monkeypatch.context() as patch:
        patch.setattr(_office.shutil, 'rmtree', deny_staging_delete)
        receipt = _office.run_ops(path, [op])
    assert receipt.startswith('✗' if fail_operation else '✓'), receipt
    assert 'cleanup failed' in receipt
    assert path.read_text() == ('before\n' if fail_operation else 'before\nafter\n')
    staging, = tmp_path.glob('.office-*')
    assert str(staging) in receipt
    if fail_operation:
        assert 'file unchanged' in receipt
        assert 'not published' in receipt
        assert 'unknown' in receipt


def test_cleanup_does_not_mask_publish_error(tmp_path, monkeypatch):
    path = tmp_path / 'original.txt'
    path.write_text('before\n')
    real_unlink = os.unlink

    def deny_backup_delete(name, *args, **kwargs):
        if Path(name).name.startswith('.office-backup-'):
            raise PermissionError('secondary cleanup failure')
        return real_unlink(name, *args, **kwargs)

    def deny_publish(*args):
        raise PermissionError('primary publish failure')

    with monkeypatch.context() as patch:
        patch.setattr(_office.os, 'unlink', deny_backup_delete)
        patch.setattr(_office.os, 'replace', deny_publish)
        receipt = _office.run_ops(path, [{'append': {'content': 'after\n'}}])
    assert receipt.startswith('✗'), receipt
    assert 'primary publish failure' in receipt
    assert 'secondary cleanup failure' in receipt
    assert 'file unchanged' in receipt
    assert path.read_text() == 'before\n'


async def test_committed_cleanup_warning_is_a_successful_tool_call(tmp_path, monkeypatch):
    from misaka.core.tools.office import create_office_tool_definition

    path = tmp_path / 'original.txt'
    path.write_text('before\n')
    real_unlink = os.unlink

    def deny_backup_delete(name, *args, **kwargs):
        if Path(name).name.startswith('.office-backup-'):
            raise PermissionError('injected backup cleanup failure')
        return real_unlink(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(_office.os, 'unlink', deny_backup_delete)
        result = await create_office_tool_definition(str(tmp_path)).execute(
            'acceptance', {'path': str(path), 'ops': [{'append': {'content': 'after\n'}}]})
    receipt = result.content[0].text
    assert receipt.startswith('✓'), receipt
    assert 'cleanup failed' in receipt
    assert path.read_text() == 'before\nafter\n'


def test_old_render_cache_does_not_hide_fixed_field_text(tmp_path, monkeypatch):
    from misaka.core.documents.office import cache

    path = package(tmp_path, '<w:fldSimple w:instr="DATE"><w:r><w:t>2026-09-15</w:t></w:r></w:fldSimple>')
    stat = path.stat()
    material = f'{os.path.realpath(path)}\0{stat.st_size}\0{stat.st_mtime_ns}'
    old_key = hashlib.sha256(material.encode('utf-8', 'surrogatepass')).hexdigest()[:32]
    directory = cache._directory(workspace=tmp_path)
    stale = directory / f'{old_key}.md'
    stale.write_text('old rendering without the date')
    actual = cache.render_cached(path, lambda: reader.render(path), workspace=tmp_path)
    assert '2026-09-15' in actual
    assert stale.read_text() == 'old rendering without the date'
    monkeypatch.setattr(cache, 'RENDER_VERSION', cache.RENDER_VERSION + 1)
    assert cache.render_cached(path, lambda: 'next renderer', workspace=tmp_path) == 'next renderer'
