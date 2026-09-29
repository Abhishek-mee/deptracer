"""Dependency sweep orchestration."""

from __future__ import annotations

import os
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import compiler, fixer, parser, resolver, tracer
from .config import load_project_config
from .licenses import scan_dependencies
from .reporting import Reporter
from .state import StateStore


def _identity(category, requested_path):
    return f"{category}:{requested_path}"


def _remove_trace_files(*paths):
    for path in paths:
        if not path:
            continue
        try:
            os.unlink(path)
        except OSError:
            pass


def _remaining(deadline):
    return None if deadline is None else deadline - time.monotonic()


def _bounded_timeout(configured, deadline):
    remaining = _remaining(deadline)
    if remaining is None:
        return configured, False
    if remaining <= 0:
        return 0.001, True
    return min(configured, remaining), remaining < configured


def _resolve_all(dependencies, project_dir, search_paths, deadline, workers, verbose):
    results = {}
    diagnostics = []
    diagnostic_lock = threading.Lock()

    def record_diagnostic(message):
        with diagnostic_lock:
            diagnostics.append(message)

    def resolve(item):
        category, requested = item
        return item, resolver.hunt_missing_library(
            os.path.basename(requested),
            project_dir,
            verbose=verbose,
            deadline=deadline,
            search_paths=search_paths,
            diagnostics=_DiagnosticSink(record_diagnostic),
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(resolve, item) for item in dependencies]
        for future in as_completed(futures):
            item, found = future.result()
            results[item] = found
    return results, diagnostics


class _DiagnosticSink:
    """Thread-safe list-like adapter used by parallel resolver workers."""

    def __init__(self, callback):
        self.callback = callback

    def append(self, message):
        self.callback(message)


def run_akdeptracer(
    project_dir,
    binary_name,
    spec_name,
    verbose=False,
    trace_timeout=60,
    build_timeout=600,
    max_iterations=10,
    max_duration=3600,
    resume=False,
    dry_run=False,
    output_format="human",
    search_paths=(),
    workers=4,
    license_policy="warn",
    keep_logs=False,
):
    reporter = Reporter(output_format)
    if platform.system().lower() != "linux":
        reporter.error("ENVIRONMENT", "PyInstaller tracing requires Linux; use 'deptracer native' for native dependency bundling.")
        return False

    root_dir = Path(project_dir).resolve()
    target_binary = root_dir / "dist" / binary_name
    spec_file = root_dir / spec_name
    if not spec_file.exists():
        reporter.error("SETUP", f"Spec file not found: {spec_file}")
        return False

    try:
        project_config = load_project_config(root_dir)
    except (OSError, ValueError) as exc:
        reporter.error("SETUP", str(exc))
        return False
    effective_search_paths = list(project_config.get("search_paths", []))
    for item in search_paths:
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = root_dir / candidate
        effective_search_paths.append(str(candidate.resolve()))

    options = {
        "trace_timeout": trace_timeout,
        "build_timeout": build_timeout,
        "max_iterations": max_iterations,
        "max_duration": max_duration,
        "dry_run": dry_run,
        "search_paths": effective_search_paths,
        "workers": workers,
        "license_policy": license_policy,
    }
    store = StateStore(root_dir)
    state = store.load() if resume else None
    if state and (state.get("binary_name") != binary_name or state.get("spec_name") != spec_name):
        reporter.error("RESUME", "Saved state belongs to a different binary or spec file.")
        return False
    if state:
        reporter.info("RESUME", f"Continuing from iteration {state.get('iteration', 1)}")
        state["status"] = "running"
    else:
        state = store.new(binary_name, spec_name, options)
        store.save(state)

    iteration = int(state.get("iteration", 1))
    injected = set(state.get("injected", []))
    system_noise = set(state.get("system_noise", []))
    started_at = time.monotonic()
    deadline = None if max_duration is None else started_at + max_duration

    def fail(stage, message, fix, **details):
        reporter.error(stage, message, **details)
        store.event(state, stage, "failed", message, fix=fix, **details)
        return False

    if not target_binary.exists() and not dry_run:
        timeout, overall_limited = _bounded_timeout(build_timeout, deadline)
        reporter.info("COMPILER", "Initial executable is missing; building it before tracing.")
        try:
            if not compiler.build(str(spec_file), iteration=1, verbose=verbose, timeout=timeout, quiet=reporter.is_json):
                return fail("COMPILER", "Initial PyInstaller build failed.", "Run with --verbose and fix the reported PyInstaller error.")
        except compiler.BuildTimeoutError as exc:
            message = "Overall time budget exhausted during initial compilation." if overall_limited else str(exc)
            return fail("BUDGET" if overall_limited else "COMPILER", message, "Increase the applicable build or overall timeout.")

    while True:
        if max_iterations is not None and iteration > max_iterations:
            return fail("BUDGET", f"Iteration budget exhausted after {max_iterations} iterations.", "Increase --max-iterations or inspect the saved iteration history.")
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            return fail("BUDGET", f"Overall time budget exhausted after {max_duration:g}s.", "Increase --max-duration or reduce the workload.")

        state["iteration"] = iteration
        store.event(state, "TRACE", "running", f"Starting iteration {iteration}")
        reporter.info("CORE", f"Starting iteration {iteration}")
        trace_timeout_now, overall_limited = _bounded_timeout(trace_timeout, deadline)
        log_path = stderr_log = None
        try:
            log_path, stderr_log, returncode = tracer.run_and_trace(
                str(target_binary),
                str(root_dir),
                verbose=verbose,
                timeout=trace_timeout_now,
            )
            if not log_path:
                return fail("TRACER", "Tracer failed to start.", "Verify that strace and Bubblewrap are installed and usable.")
            dependencies = parser.extract_missing_libraries_and_hidden_imports(
                log_path,
                stderr_log,
                verbose=verbose and not reporter.is_json,
                deadline=deadline,
            )
        except tracer.TraceTimeoutError as exc:
            message = "Overall time budget exhausted during tracing." if overall_limited else str(exc)
            return fail("BUDGET" if overall_limited else "TRACER", message, "Increase the applicable trace or overall timeout, or make the application non-interactive.")
        except tracer.TraceLaunchError as exc:
            return fail("TRACER", str(exc), "Verify that strace and Bubblewrap are installed and permitted by the kernel.")
        except parser.ParseTimeoutError as exc:
            return fail("BUDGET", str(exc), "Increase --max-duration or reduce trace volume.")
        except (OSError, UnicodeError) as exc:
            return fail("PARSER", f"Could not parse the trace output: {exc}", "Retain logs with --keep-logs and inspect the strace output.")
        finally:
            if not keep_logs:
                _remove_trace_files(log_path, stderr_log)

        if not any(dependencies.values()):
            if returncode:
                return fail("APPLICATION", f"No missing dependency was detected, but the application exited with code {returncode}.", "Run the binary directly and fix its application error.")
            reporter.success("CORE", "Executable completed cleanly in the isolation sandbox.")
            store.event(state, "SUCCESS", "success", "Executable completed cleanly in the isolation sandbox.")
            return True

        requested = []
        for category in ("binaries", "data"):
            for path in dependencies[category]:
                key = _identity(category, path)
                if key in injected:
                    return fail("DEADLOCK", f"Previously injected dependency is still missing: {path}", "Inspect its destination in the spec and the runtime lookup path.")
                if key not in system_noise:
                    requested.append((category, path))

        try:
            resolved, resolution_diagnostics = _resolve_all(
                requested,
                str(root_dir),
                effective_search_paths,
                deadline,
                workers,
                verbose and not reporter.is_json,
            )
        except resolver.ResolutionTimeoutError as exc:
            return fail("BUDGET", str(exc), "Increase --max-duration or narrow the search paths.")

        for diagnostic in resolution_diagnostics:
            reporter.warning("RESOLVER", diagnostic)

        payload = {"binaries": [], "data": [], "hidden_imports": []}
        unresolved = []
        for (category, requested_path), found in resolved.items():
            key = _identity(category, requested_path)
            if found:
                payload[category].append((requested_path, found))
                injected.add(key)
                reporter.info("RESOLVER", f"Resolved {requested_path}", source=found)
            else:
                system_noise.add(key)
                unresolved.append(requested_path)
                reporter.warning("RESOLVER", f"Could not resolve {requested_path}")

        for module in dependencies["hidden_imports"]:
            key = _identity("hidden_imports", module)
            if key in injected:
                return fail("DEADLOCK", f"Previously injected hidden import is still missing: {module}", "Verify that the module is installed and importable in the build environment.")
            payload["hidden_imports"].append(module)
            injected.add(key)

        resolved_paths = [source for category in ("binaries", "data") for _, source in payload[category]]
        findings = scan_dependencies(resolved_paths) if license_policy != "ignore" else []
        for finding in findings:
            reporter.warning("LICENSE", f"Potential {finding.risk.upper()} dependency: {finding.path}", evidence=finding.evidence)
        if findings and license_policy == "error":
            return fail("LICENSE", "Copyleft-licensed dependency blocked by policy.", "Review redistribution obligations or use --license-policy warn after approval.", findings=[item.as_dict() for item in findings])

        if deadline is not None and time.monotonic() >= deadline:
            return fail("BUDGET", "Overall time budget exhausted during dependency analysis.", "Increase --max-duration or reduce the dependency set.")

        total_changes = sum(len(items) for items in payload.values())
        if not total_changes:
            return fail("RESOLVER", "No missing dependencies could be resolved.", "Install the missing files or add their directories with --search-path.", unresolved=unresolved)
        try:
            changes = fixer.patch_spec_file(
                str(spec_file),
                payload,
                probe_path=None,
                dry_run=dry_run,
            )
        except (OSError, SyntaxError, fixer.SpecPatchError) as exc:
            return fail("FIXER", str(exc), "Use a standard PyInstaller Analysis spec or adjust the spec manually.")

        if dry_run:
            reporter.success("DRY_RUN", "Planned spec changes without modifying or rebuilding.", changes=changes, unresolved=unresolved)
            store.event(state, "DRY_RUN", "success", "Dry run completed.", changes=changes, unresolved=unresolved)
            return not unresolved

        reporter.info("FIXER", "Spec file patched.", changes=changes)
        timeout, overall_limited = _bounded_timeout(build_timeout, deadline)
        try:
            if not compiler.build(str(spec_file), iteration=iteration, verbose=verbose, timeout=timeout, quiet=reporter.is_json):
                return fail("COMPILER", "PyInstaller build failed.", "Run with --verbose and fix the reported PyInstaller error.")
        except compiler.BuildTimeoutError as exc:
            message = "Overall time budget exhausted during compilation." if overall_limited else str(exc)
            return fail("BUDGET" if overall_limited else "COMPILER", message, "Increase the applicable build or overall timeout.")

        iteration += 1
        state["iteration"] = iteration
        state["injected"] = sorted(injected)
        state["system_noise"] = sorted(system_noise)
        store.event(state, "COMPILER", "running", "Build completed; another verification sweep is required.", changes=changes, unresolved=unresolved)
