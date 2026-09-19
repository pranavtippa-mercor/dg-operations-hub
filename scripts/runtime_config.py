"""Resolve private runtime paths consistently on local and hosted checkouts."""
import copy
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize_config(config, root=None):
    if not isinstance(config, dict):
        raise ValueError('Runtime configuration must be an object.')
    result = copy.deepcopy(config)
    root = Path(root or ROOT).resolve()

    def resolve(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Runtime paths must be nonempty strings.')
        path = Path(value).expanduser()
        return str((path if path.is_absolute() else root/path).resolve())

    for name in ('task_state', 'source_lock'):
        if name in result:
            result[name] = resolve(result[name])
    for section, names in (('modules', ('cache_dir', 'data')), ('slack', ('cache_dir',))):
        if section not in result:
            continue
        if not isinstance(result[section], dict):
            raise ValueError('Runtime collector settings must be objects.')
        for name in names:
            if name in result[section]:
                result[section][name] = resolve(result[section][name])
    mode = result.get('runtime_mode', 'Independent collectors')
    if not isinstance(mode, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 .()_-]{0,79}', mode):
        raise ValueError('Runtime mode must be a short plain-text label.')
    result['runtime_mode'] = mode
    return result


def load_config(path, root=None):
    root = Path(root or ROOT).resolve()
    path = Path(path).expanduser()
    return normalize_config(json.loads((path if path.is_absolute() else root/path).read_text()), root=root)
