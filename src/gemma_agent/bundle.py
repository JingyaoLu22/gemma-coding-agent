"""Build a validated runtime bundle without uploading it anywhere."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import zipfile

from .validation import CheckError, iter_agent_files, validate, validate_zip


def build_bundle(source: Path, output: Path, runtime: bool = False) -> list[str]:
    source = source.resolve()
    output = output.absolute()
    if output.resolve().is_relative_to(source):
        raise CheckError('bundle output must be outside the agent directory')
    if output.is_symlink():
        raise CheckError('bundle output must not be a symlink')
    report = validate(source, runtime=runtime)
    if report.errors:
        raise CheckError('\n'.join(report.errors))
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.gemma-agent-', suffix='.zip', dir=output.parent)
    os.close(fd)
    names = []
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in iter_agent_files(source):
                name = path.relative_to(source).as_posix()
                archive.write(path, name)
                names.append(name)
        report = validate_zip(Path(temporary), runtime=runtime)
        if report.errors:
            raise CheckError('\n'.join(report.errors))
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return names
