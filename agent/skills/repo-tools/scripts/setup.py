"""Install the helper in task-local git metadata; run with no skill args."""
from pathlib import Path
import hashlib
import json
import os


def find_root():
    # ADK materializes scripts into a temporary cwd. Shell PWD / PYTHONPATH
    # retain the actual sandbox workspace while skill resources are temporary.
    candidates = [os.environ.get('PWD', '')]
    candidates += os.environ.get('PYTHONPATH', '').split(os.pathsep)
    candidates += ['/workspace', os.getcwd()]
    for value in candidates:
        if value:
            root = Path(value).resolve()
            if (root / '.git').is_dir():
                return root
    raise ValueError('cannot locate task workspace; use built-in tools instead')


def install(root):
    root = Path(root).resolve()
    metadata = root / '.git'
    if not metadata.is_dir() or metadata.is_symlink():
        raise ValueError('expected a task-local .git directory')
    source = Path(__file__).with_name('repo_tools.py')
    target = metadata / 'swe_agent.py'
    data = source.read_bytes()
    if target.is_symlink():
        raise ValueError('helper destination is a symlink')
    if not target.exists() or target.read_bytes() != data:
        # All installed files stay out of the submitted source diff.
        temporary = metadata / 'swe_agent.installing'
        temporary.write_bytes(data)
        os.replace(temporary, target)
    return {'status': 'ok', 'command': 'python .git/swe_agent.py',
            'sha256': hashlib.sha256(data).hexdigest(),
            'memory': 'task-specific temporary directory; find/edit/check record results automatically'}


if __name__ == '__main__':
    try:
        print(json.dumps(install(find_root())))
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc),
                          'fallback': 'Use run_command and edit_file directly; do not repeatedly reinstall.'}))
        raise SystemExit(1)
