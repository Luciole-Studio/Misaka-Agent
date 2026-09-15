"""Native environment and permission ceiling for tool-free ACP auxiliary calls."""
import os


def get_read_block_error(path):
    return 'LCM auxiliary inference has no filesystem tool grant'


def get_write_denied_error(path):
    return 'LCM auxiliary inference has no filesystem tool grant'


def is_write_approval_required(path):
    return True


def hermes_subprocess_env(*, inherit_credentials=False):
    from ..native.acp_env import (
        _ALWAYS_STRIP_KEYS,
        _HERMES_PROVIDER_ENV_FORCE_PREFIX,
        _is_hermes_internal_secret,
    )
    # Native children never inherit a Board/session writer identity. The original
    # tier-1 filter remains; ACP owns its own configured provider login.
    env = {key: value for key, value in os.environ.items() if key not in _ALWAYS_STRIP_KEYS
           and not key.startswith(('MISAKA_', 'HERMES_', _HERMES_PROVIDER_ENV_FORCE_PREFIX))
           and not _is_hermes_internal_secret(key)}
    env.setdefault('PYTHONUTF8', '1')
    return env


def windows_hide_flags():
    return 0x08000000 if os.name == 'nt' else 0
