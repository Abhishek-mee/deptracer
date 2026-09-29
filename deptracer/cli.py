"""Command-line interface for deptracer."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, resolver
from .core import run_akdeptracer
from .explain import explain_problem
from .native import NativeDependencyError, bundle_native, inspect_dependencies, is_system_dependency


def positive_number(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def positive_integer(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _build_parser():
    parser = argparse.ArgumentParser(prog="deptracer", description="Runtime dependency tracer and native dependency bundler")
    parser.add_argument("--version", action="version", version=f"deptracer v{__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable detailed engine output")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Run the PyInstaller self-healing loop on Linux")
    build.add_argument("project_dir")
    build.add_argument("binary_name")
    build.add_argument("spec_name")
    build.add_argument("--trace-timeout", type=positive_number, default=60, metavar="SECONDS")
    build.add_argument("--build-timeout", type=positive_number, default=600, metavar="SECONDS")
    build.add_argument("--max-iterations", type=positive_integer, default=10, metavar="COUNT")
    build.add_argument("--max-duration", type=positive_number, default=3600, metavar="SECONDS")
    build.add_argument("--resume", action="store_true", help="Continue from project-local saved state")
    build.add_argument("--dry-run", action="store_true", help="Report spec changes without writing or rebuilding")
    build.add_argument("--output-format", choices=("human", "json"), default="human")
    build.add_argument("--search-path", action="append", default=[], help="Additional dependency search directory; repeatable")
    build.add_argument("--workers", type=positive_integer, default=4, help="Parallel dependency lookup workers")
    build.add_argument("--license-policy", choices=("ignore", "warn", "error"), default="warn")
    build.add_argument("--keep-logs", action="store_true", help="Retain strace and stderr logs for debugging")

    explain = commands.add_parser("explain", help="Explain the latest project-local run")
    explain.add_argument("project_dir", nargs="?", default=".")
    explain.add_argument("--output-format", choices=("human", "json"), default="human")

    native = commands.add_parser("native", help="Inspect and bundle ELF, PE/DLL, or Mach-O dependencies")
    native.add_argument("binary")
    native.add_argument("--output-dir", default="dist-native")
    native.add_argument("--search-path", action="append", default=[])
    native.add_argument("--dry-run", action="store_true")
    native.add_argument("--codesign", action="store_true", help="Ad-hoc sign the patched macOS binary")
    native.add_argument("--license-policy", choices=("ignore", "warn", "error"), default="warn")
    native.add_argument("--output-format", choices=("human", "json"), default="human")
    return parser


def _native_command(args):
    binary = Path(args.binary).resolve()
    if not binary.exists():
        print(f"Binary not found: {binary}", file=sys.stderr)
        return 1
    try:
        queue = list(inspect_dependencies(binary))
        resolved = []
        seen = set()
        dynamic_search_paths = [*args.search_path, str(binary.parent)]
        while queue:
            dependency = queue.pop(0)
            key = os.path.normcase(os.path.basename(dependency))
            if key in seen:
                continue
            seen.add(key)
            if is_system_dependency(dependency):
                resolved.append(dependency)
                continue
            if os.path.isabs(dependency):
                found = dependency
            else:
                found = resolver.hunt_missing_library(
                    dependency,
                    str(binary.parent),
                    search_paths=dynamic_search_paths,
                )
            resolved.append((dependency, found) if found else dependency)
            if found and os.path.exists(found):
                dynamic_search_paths.append(str(Path(found).parent))
                try:
                    queue.extend(inspect_dependencies(found))
                except NativeDependencyError:
                    pass
        result = bundle_native(binary, resolved, args.output_dir, dry_run=args.dry_run, codesign=args.codesign)
    except NativeDependencyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.license_policy == "ignore":
        result["licenses"] = []
    blocked = bool(result["licenses"] and args.license_policy == "error")
    if args.output_format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Target binary: {result['binary']}")
        for action in result["actions"]:
            destination = f" -> {action['destination']}" if "destination" in action else ""
            print(f"- {action['status']}: {action['source']}{destination}")
        for finding in result["licenses"]:
            print(f"- license warning: {finding['risk'].upper()} {finding['path']}")
    unresolved = any(action["status"] == "unresolved" for action in result["actions"])
    return 1 if blocked or unresolved else 0


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "build":
        succeeded = run_akdeptracer(
            args.project_dir,
            args.binary_name,
            args.spec_name,
            verbose=args.verbose,
            trace_timeout=args.trace_timeout,
            build_timeout=args.build_timeout,
            max_iterations=args.max_iterations,
            max_duration=args.max_duration,
            resume=args.resume,
            dry_run=args.dry_run,
            output_format=args.output_format,
            search_paths=args.search_path,
            workers=args.workers,
            license_policy=args.license_policy,
            keep_logs=args.keep_logs,
        )
        return 0 if succeeded else 1
    if args.command == "explain":
        return 0 if explain_problem(args.project_dir, args.output_format) else 1
    if args.command == "native":
        return _native_command(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
