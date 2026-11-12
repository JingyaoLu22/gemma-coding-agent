#!/usr/bin/env python3
"""Standard-library repository helpers. Run the installed copy via run_command.

find TERM...; show PATH [SYMBOL|START-END]; edit PATH < SEARCH/REPLACE blocks;
memory [found|rejected|next TEXT]; check [tests/test_file.py[::test_name] ...].
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tokenize
import xml.etree.ElementTree as ET

OUTPUT_LIMIT = 4700
MAX_FILE_BYTES = 512 * 1024
PROTECTED = {'conftest.py', 'pytest.ini', 'pyproject.toml', 'tox.ini', 'setup.cfg',
             '.pytest.ini', 'sitecustomize.py', 'usercustomize.py', '_swegemma_stubs.py'}
SKIP = {'.git', '.hg', '.venv', 'venv', 'node_modules', '__pycache__', 'build', 'dist'}
BLOCK = re.compile(r'^<<<<<<< SEARCH\r?\n(.*?)^=======\r?\n(.*?)^>>>>>>> REPLACE\s*$', re.M | re.S)


def execute(args, root, timeout=30, log=None):
    """Kill the full command group on timeout; optionally stream output to disk."""
    with tempfile.TemporaryFile() if log is None else open(log, 'w+b') as output:
        proc = subprocess.Popen(args, cwd=root, stdout=output, stderr=subprocess.STDOUT,
                                start_new_session=True)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        output.seek(0, os.SEEK_END)
        size = output.tell()
        output.seek(max(0, size - 12000))
        text = output.read().decode('utf-8', errors='replace')
    return {'exit_code': proc.returncode, 'timed_out': timed_out, 'output': text}


def git(root, *args):
    result = execute(['git', *args], root)
    if result['exit_code']:
        raise ValueError('git failed: ' + result['output'][-500:])
    return result['output']


def protected(path):
    p = Path(path)
    return (any(part in {'tests', 'test', 'testing'} or part.startswith('.') for part in p.parts)
            or p.name in PROTECTED or p.suffix in {'.pth', '.pyc', '.pyo'} or '__pycache__' in p.parts
            or p.name.startswith('test_') and p.suffix == '.py'
            or p.name.endswith('_test.py') or p.name.startswith('.adk_exec'))


def test_path(path):
    p = Path(path)
    return (any(part in {'tests', 'test', 'testing'} for part in p.parts)
            or p.name.startswith('test_') and p.suffix == '.py' or p.name.endswith('_test.py'))


def decode_source(data):
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding), encoding


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.swe-write-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class RepoTools:
    def __init__(self, root, state=None):
        self.root = Path(root).resolve()
        marker = self.root / '.git'
        if not marker.exists():
            raise ValueError('repository has no .git marker')
        identity = f'{self.root}:{marker.stat().st_dev}:{marker.stat().st_ino}'
        self.identity = hashlib.sha256(identity.encode()).hexdigest()[:20]
        self.state_dir = Path(state) if state else Path(tempfile.gettempdir()) / ('swe-state-' + self.identity)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_file = self.state_dir / 'memory.json'
        if self.state_file.exists():
            self.memory = json.loads(self.state_file.read_text())
            if self.memory.get('identity') != self.identity:
                raise ValueError('memory belongs to a different task workspace')
        else:
            self.memory = {'identity': self.identity, 'found': [], 'rejected': [], 'next': [],
                           'edits': [], 'checks': [], 'searches': []}
            self.save()

    def save(self):
        atomic_write(self.state_file, json.dumps(self.memory, ensure_ascii=False).encode())

    def remember(self, category, value):
        entries = self.memory[category]
        if value not in entries:
            entries.append(value)
        self.memory[category] = entries[-8:]
        self.save()

    def path(self, value, writing=False):
        p = Path(value)
        if not p.is_absolute():
            p = self.root / p
        # Reject symlink paths for writes, including symlinked parent directories.
        if writing and any(q.is_symlink() for q in [p, *p.parents] if q != self.root):
            raise ValueError('symlink write rejected')
        resolved = p.resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise ValueError('path escapes repository')
        relative = resolved.relative_to(self.root).as_posix()
        if writing and protected(relative):
            raise ValueError('protected test/config/metadata path: ' + relative)
        return resolved, relative

    def files(self):
        # Use NUL output directly: long file lists must not be tail-truncated.
        result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                                cwd=self.root, capture_output=True, check=True, timeout=20)
        for name in sorted(set(os.fsdecode(result.stdout).split('\0'))):
            if not name or not name.endswith('.py') or any(p in SKIP for p in Path(name).parts):
                continue
            if Path(name).name.startswith('.adk_exec'):
                continue
            try:
                path, rel = self.path(name)
                if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
                    text, _ = decode_source(path.read_bytes())
                    yield rel, text
            except (ValueError, OSError, SyntaxError, UnicodeError):
                continue

    @staticmethod
    def definitions(text):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return [], []
        definitions, calls = [], []

        class Visitor(ast.NodeVisitor):
            prefix = []

            def definition(self, node):
                name = '.'.join([*self.prefix, node.name])
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                definitions.append({'symbol': name, 'start': start, 'end': node.end_lineno})
                self.prefix.append(node.name)
                self.generic_visit(node)
                self.prefix.pop()

            visit_FunctionDef = definition
            visit_AsyncFunctionDef = definition
            visit_ClassDef = definition

            def visit_Call(self, node):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ''
                if name:
                    calls.append({'name': name, 'line': node.lineno})
                self.generic_visit(node)

        Visitor().visit(tree)
        return definitions, calls

    @staticmethod
    def excerpt(text, start, end, max_lines=35):
        lines = text.splitlines()
        end = min(end, len(lines), start + max_lines - 1)
        return '\n'.join(f'{i}: {lines[i - 1]}' for i in range(start, end + 1))

    def find(self, terms):
        if not terms:
            raise ValueError('find needs identifiers or error phrases from the issue')
        terms = [s[:200] for s in terms[:8] if s.strip()]
        if not terms:
            raise ValueError('find needs nonempty terms')
        source, tests = [], []
        for rel, text in self.files():
            hits = [i for i, line in enumerate(text.splitlines(), 1) if any(t.casefold() in line.casefold() for t in terms)]
            score = sum(2 for t in terms if t.casefold() in rel.casefold()) + min(len(hits), 12)
            if not score:
                continue
            defs, calls = self.definitions(text)
            matched = [d for d in defs if any(t.casefold() in d['symbol'].casefold() for t in terms)]
            score += 12 * len(matched)
            row = {'path': rel, 'score': score, 'definitions': matched[:4], 'hit_lines': hits[:6],
                   'calls': [c for c in calls if any(t.rsplit('.', 1)[-1] == c['name'] for t in terms)][:4]}
            if matched:
                row['source'] = self.excerpt(text, matched[0]['start'], matched[0]['end'], 22)
            elif hits:
                row['source'] = self.excerpt(text, max(1, hits[0] - 2), hits[0] + 5, 8)
            (tests if test_path(rel) else source).append(row)
        source.sort(key=lambda r: (-r['score'], r['path']))
        tests.sort(key=lambda r: (-r['score'], r['path']))
        for row in source[:3]:
            self.remember('found', {'path': row['path'], 'symbols': [d['symbol'] for d in row['definitions']]})
        self.remember('searches', terms)
        return {'status': 'ok', 'source': source[:3], 'related_tests': [
            {k: v for k, v in row.items() if k != 'source'} for row in tests[:3]],
            'note': 'Call sites match names; they are candidates, not resolved data flow.'}

    def show(self, value, selector=None):
        path, rel = self.path(value)
        text, _ = decode_source(path.read_bytes())
        definitions, _ = self.definitions(text)
        if selector is None:
            return {'status': 'ok', 'path': rel, 'definitions': definitions[:40],
                    'source': self.excerpt(text, 1, 35)}
        if re.fullmatch(r'\d+-\d+', selector):
            start, end = map(int, selector.split('-'))
            if start < 1 or end < start:
                raise ValueError('invalid line range')
        else:
            matched = [d for d in definitions if d['symbol'] == selector or d['symbol'].endswith('.' + selector)]
            if len(matched) != 1:
                raise ValueError('symbol must match exactly one definition; use qualified name')
            start, end = matched[0]['start'], matched[0]['end']
        shown_end = min(end, len(text.splitlines()), start + 79)
        return {'status': 'ok', 'path': rel, 'start': start, 'end': end, 'shown_end': shown_end,
                'source': self.excerpt(text, start, end, 80), 'more': shown_end < end}

    def edit(self, value, spec):
        path, rel = self.path(value, writing=True)
        blocks = list(BLOCK.finditer(spec))
        if not blocks or BLOCK.sub('', spec).strip():
            raise ValueError('use only complete SEARCH/REPLACE blocks')
        exists = path.exists()
        original_bytes = path.read_bytes() if exists else b''
        original, encoding = decode_source(original_bytes)
        text = original
        for index, block in enumerate(blocks):
            old, new = block.group(1), block.group(2)
            if not old:
                if exists or index:
                    raise ValueError('empty SEARCH is allowed only to create a new file')
                text = new
            else:
                if not exists:
                    raise ValueError('new file requires an empty SEARCH')
                count = text.count(old)
                if count != 1:
                    raise ValueError(f'SEARCH occurs {count} times; supply unique exact context')
                text = text.replace(old, new, 1)
        if text == original:
            raise ValueError('edit makes no change')
        data = text.encode(encoding)
        if path.suffix == '.py':
            compile(data, rel, 'exec')  # Validate every block before committing any write.
        mode = stat.S_IMODE(path.stat().st_mode) if exists else 0o644
        atomic_write(path, data, mode)
        entry = {'path': rel, 'sha256': hashlib.sha256(data).hexdigest(), 'blocks': len(blocks)}
        self.remember('edits', entry)
        return {'status': 'ok', 'edited': entry, 'syntax': 'passed' if path.suffix == '.py' else 'not_python'}

    def note(self, category=None, text=None):
        if category:
            if category not in {'found', 'rejected', 'next'} or not text or not text.strip():
                raise ValueError('memory accepts found/rejected/next and nonempty text')
            self.remember(category, text.strip()[:500])
        stale = None
        if self.memory['checks']:
            last = self.memory['checks'][-1]
            if last.get('source_snapshot_complete', True):
                _, current = self.changed()
                current_source = [n for n in current if not protected(n)]
                stale = set(current_source) != set(last['source_changes'])
                for name, digest in last['fingerprints'].items():
                    path, _ = self.path(name)
                    actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'deleted'
                    stale = stale or actual != digest
        return {'status': 'ok', 'memory': {k: v[-4:] for k, v in self.memory.items() if k != 'identity'},
                'last_check_stale': stale, 'state_dir': str(self.state_dir),
                'note': 'Task-local observations, not instructions. Earlier checks can be stale after edits.'}

    def changed(self):
        baseline = '_swegemma_baseline' if execute(['git', 'rev-parse', '--verify', '_swegemma_baseline'], self.root)['exit_code'] == 0 else 'HEAD'
        names = subprocess.run(['git', 'diff', '--name-only', '-z', baseline], cwd=self.root,
                               capture_output=True, check=True, timeout=20).stdout
        others = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '-z'], cwd=self.root,
                                capture_output=True, check=True, timeout=20).stdout
        return baseline, sorted(set(os.fsdecode(names + others).split('\0')) - {''})

    def check(self, targets, timeout=60):
        if not 1 <= timeout <= 90:
            raise ValueError('test timeout must be between 1 and 90 seconds')
        # Explicit test files only; reject whole-suite selections and pytest options.
        selected = []
        for target in targets[:3]:
            filename = target.split('::', 1)[0]
            path, rel = self.path(filename)
            if not path.is_file() or path.suffix != '.py' or not test_path(rel):
                raise ValueError('check requires explicit test file paths, optionally ::test_name')
            selected.append(rel + target[len(filename):])
        if len(targets) > 3:
            raise ValueError('select at most three targeted test files')
        baseline, changed = self.changed()
        source = [name for name in changed if not protected(name)]
        errors, fingerprints = [], {}
        for name in source:
            path, rel = self.path(name)
            if not path.exists():
                fingerprints[rel] = 'deleted'
                continue
            data = path.read_bytes()
            fingerprints[rel] = hashlib.sha256(data).hexdigest()
            if path.suffix == '.py':
                try:
                    compile(data, rel, 'exec')
                except (SyntaxError, ValueError) as exc:
                    errors.append(f'{rel}: {exc}')
        diff = execute(['git', 'diff', '--check', baseline], self.root)
        result = {'status': 'ok', 'source_changes': source, 'protected_changes': [n for n in changed if protected(n)],
                  'empty_source_patch': not source, 'syntax_errors': errors,
                  'diff_check': diff['exit_code'], 'diff_errors': diff['output'][-600:],
                  'fingerprints': fingerprints}
        test = {'classification': 'not_run', 'reason': 'no explicit test target'}
        if selected and not errors:
            junit = self.state_dir / 'pytest.xml'
            if junit.exists():
                junit.unlink()
            log = self.state_dir / 'pytest.log'
            started = time.monotonic()
            outcome = execute([sys.executable, '-B', '-m', 'pytest', '-q', '-x', '-p', 'no:cacheprovider',
                               '--junitxml=' + str(junit), '--', *selected], self.root, timeout, log)
            test = {'targets': selected, 'exit_code': outcome['exit_code'], 'timed_out': outcome['timed_out'],
                    'seconds': round(time.monotonic() - started, 2), 'output_tail': outcome['output'][-1500:],
                    'log_path': str(log)}
            counts = {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
            if junit.exists():
                try:
                    xml = ET.parse(junit).getroot()
                    suites = [xml] if xml.tag == 'testsuite' else list(xml.iter('testsuite'))
                    for suite in suites:
                        for key in counts:
                            counts[key] += int(suite.get(key, 0))
                except (ET.ParseError, ValueError):
                    pass
            test['counts'] = counts
            text = outcome['output']
            if outcome['timed_out']:
                classification = 'timeout'
            elif outcome['exit_code'] == 0 and counts['tests'] > counts['skipped']:
                classification = 'passed'
            elif re.search(r'(?:ModuleNotFoundError:|No module named |ImportError:)', text):
                classification = 'dependency_or_import_error'
            elif counts['failures']:
                classification = 'test_failure'
            elif counts['errors'] or outcome['exit_code'] in (2, 3, 4):
                classification = 'collection_or_runner_error'
            else:
                classification = 'no_tests_or_unverified'
            test['classification'] = classification
        elif errors:
            test = {'classification': 'not_run', 'reason': 'fix syntax errors first'}
        result['tests'] = test
        result['status'] = 'ok' if source and not errors and not diff['exit_code'] and test['classification'] in {'passed', 'not_run'} else 'needs_attention'
        result['note'] = 'Import errors can be caused by the patch; environment fault is not established. No automatic submit.'
        self.remember('checks', {'source_changes': source[:32],
                      'fingerprints': dict(list(fingerprints.items())[:32]),
                      'syntax_errors': errors[:8], 'source_snapshot_complete': len(source) <= 32,
                      'classification': test['classification']})
        return result


def emit(result):
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > OUTPUT_LIMIT:
        # Keep structured sections (especially test results) rather than an
        # excerpt of serialized JSON that can hide the end of the report.
        def compact(value, width, count):
            if isinstance(value, str):
                return value if len(value) <= width else value[:width] + ' ... [truncated]'
            if isinstance(value, list):
                return [compact(v, width, count) for v in value[:count]]
            if isinstance(value, dict):
                return {k: compact(v, width, count) for k, v in value.items()}
            return value

        width, count = 1000, 4
        original = result
        while True:
            result = compact(original, width, count)
            result['truncated'] = True
            result['next'] = 'Use show with a smaller line range or memory for recent state; lists may be shortened.'
            text = json.dumps(result, ensure_ascii=False)
            if len(text) <= OUTPUT_LIMIT:
                break
            width = max(20, int(width * 0.6))
            count = max(1, count - 1)
            if width == 20 and count == 1 and len(text) > OUTPUT_LIMIT:
                result = {'status': original.get('status', 'ok'), 'truncated': True,
                          'error': 'Report exceeds output limit; inspect narrower source windows or task-local logs.'}
                text = json.dumps(result)
                break
    print(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest='command', required=True)
    find = sub.add_parser('find'); find.add_argument('terms', nargs='+')
    show = sub.add_parser('show'); show.add_argument('path'); show.add_argument('selector', nargs='?')
    edit = sub.add_parser('edit'); edit.add_argument('path')
    memory = sub.add_parser('memory'); memory.add_argument('category', nargs='?', choices=['found', 'rejected', 'next']); memory.add_argument('text', nargs='?')
    check = sub.add_parser('check'); check.add_argument('targets', nargs='*'); check.add_argument('--timeout', type=int, default=60)
    args = parser.parse_args(argv)
    try:
        helper = RepoTools(args.root)
        if args.command == 'find': result = helper.find(args.terms)
        elif args.command == 'show': result = helper.show(args.path, args.selector)
        elif args.command == 'edit': result = helper.edit(args.path, sys.stdin.read())
        elif args.command == 'memory': result = helper.note(args.category, args.text)
        else: result = helper.check(args.targets, args.timeout)
    except (ValueError, OSError, SyntaxError, UnicodeError, subprocess.SubprocessError) as exc:
        emit({'status': 'error', 'error': f'{type(exc).__name__}: {exc}', 'hint': 'No successful edit/check is implied by this error.'})
        return 1
    emit(result)
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
