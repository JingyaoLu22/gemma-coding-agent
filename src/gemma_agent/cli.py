"""Command-line entry point for inspecting, validating and building the agent."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from . import __version__
from .bundle import build_bundle
from .validation import CheckError, DEFAULT_AGENT_DIR, load_yaml, validate, validate_zip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='gemma-agent', description='Gemma coding agent configuration and runtime bundles.')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    info = commands.add_parser('info', help='show model, tools and per-task limits')
    info.add_argument('--agent', type=Path, default=DEFAULT_AGENT_DIR)
    check = commands.add_parser('validate', help='validate an agent directory or ZIP bundle')
    check.add_argument('path', type=Path, nargs='?', default=DEFAULT_AGENT_DIR)
    check.add_argument('--runtime', action='store_true', help='also compile with installed ADK/SWE runtime dependencies')
    build = commands.add_parser('build', help='build a validated ZIP for an external runtime')
    build.add_argument('--agent', type=Path, default=DEFAULT_AGENT_DIR)
    build.add_argument('--output', type=Path, default=Path('build/gemma-agent.zip'))
    build.add_argument('--runtime', action='store_true', help='also compile with installed ADK/SWE runtime dependencies')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            report = validate_zip(args.path, runtime=args.runtime) if args.path.suffix == '.zip' else validate(args.path, runtime=args.runtime)
            report.print()
            return 1 if report.errors else 0
        if args.command == 'build':
            names = build_bundle(args.agent, args.output, runtime=args.runtime)
            print(f'Built {args.output.resolve()} ({len(names)} files)')
            print(f'sha256 {hashlib.sha256(args.output.read_bytes()).hexdigest()}')
            return 0
        report = validate(args.agent)
        if report.errors:
            report.print()
            return 1
        config = load_yaml(args.agent / 'agent.yaml', args.agent)
        budget = load_yaml(args.agent / 'eval_config.yaml', args.agent) if (args.agent / 'eval_config.yaml').exists() else {}
        print(json.dumps({'name': config['name'], 'model': config['model'], 'tools': config.get('tools', []),
                          'skills': config.get('skills', []),
                          'sampling': config.get('generate_content_config', {}),
                          'limits': budget.get('evaluation', {})}, indent=2))
        return 0
    except (CheckError, OSError) as exc:
        print(f'Error: {exc}')
        return 1
