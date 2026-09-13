"""Generation admission for Skill writers and an explicit, quiescent cutover.

Existing owners keep their captured generation. The operator preflight rejects
legacy MISAKA processes (which predate this handshake) before any activation.
This is a cooperative application fence, not protection against arbitrary shell
writes by the same OS user. No live owner is terminated or restarted here.
"""
import hashlib
import json
import os
import uuid
from pathlib import Path

ENGINE = 'hermes-f03ed94a-misaka-skill-v3'
MARKER = '.writer-generation.json'


def token(profile):
    if profile is None:
        return None
    path = Path(profile) / 'skills' / MARKER
    from .write import _safe_parents
    _safe_parents(path.parent)
    if path.is_symlink():
        raise ValueError('Skill writer generation is a symlink.')
    if not path.exists():
        return None
    content = path.read_bytes()
    record = json.loads(content)
    if not isinstance(record, dict) or record.get('version') != 1 or not isinstance(record.get('engine'), str) or not record.get('epoch'):
        raise ValueError('Invalid Skill writer generation; existing bytes preserved.')
    return hashlib.sha256(content).hexdigest()


def check_write(profile, *, activating=False):
    from .scope import _current
    profile = Path(profile).absolute()
    scope = _current.get()
    current = token(profile)
    if scope is not None and scope.profile == profile and scope.generation != current:
        raise ValueError('This Skill owner belongs to an older generation; start a new session.')
    if current and not activating:
        record = json.loads((profile / 'skills' / MARKER).read_text())
        if record.get('engine') != ENGINE:
            raise ValueError('The physical Skill tree belongs to another writer implementation.')


def _live_misaka_processes():
    import psutil
    processes = []
    for process in psutil.process_iter(['pid', 'cmdline', 'create_time']):
        if process.pid == os.getpid():
            continue
        argv = process.info.get('cmdline') or []
        if any(arg == 'misaka' or Path(arg).name == 'misaka' for arg in argv[:4]):
            processes.append({'pid': process.pid, 'start': process.info['create_time']})
    return processes


def status(profile):
    from . import write
    profile = Path(profile)
    return {'success': True, 'engine': ENGINE, 'generation': token(profile),
            'root_digest': write.digest(profile / 'skills'), 'live_owners': _live_misaka_processes(),
            'boundary': 'cooperative-generation-fence; legacy owners must be quiescent before activation'}


def activate(scope, expected_digest):
    """Called only on an owned staging tree while the shared legacy writer lock is held."""
    from misaka.utils import atomic

    from . import write
    from .scope import _skills_dir
    if not expected_digest or write.digest(scope.profile / 'skills') != expected_digest:
        raise ValueError('Cutover requires the unchanged digest from generation-status.')
    if _live_misaka_processes():
        raise ValueError('MISAKA owners are still running; no generation was changed.')
    if scope.storage is None:
        raise ValueError('Generation activation requires a staged Skill transaction.')
    marker = {'version': 1, 'engine': ENGINE, 'epoch': uuid.uuid4().hex}
    atomic.write_text(_skills_dir() / MARKER, json.dumps(marker, sort_keys=True))
    return {'success': True, 'writer': marker}
