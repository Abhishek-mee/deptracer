"""Cross-platform dependency resolution with deadline-aware searches."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .config import discover_search_paths


class ResolutionTimeoutError(TimeoutError):
    """Raised when dependency resolution exhausts the overall sweep budget."""


def _check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ResolutionTimeoutError("Overall time budget expired during dependency resolution")


def safe_realpath(path: str, max_depth=40) -> str:
    """Resolve symlinks without hanging on circular chains."""
    current = Path(path)
    seen = set()
    for _ in range(max_depth):
        absolute = os.path.abspath(current)
        key = os.path.normcase(absolute)
        if key in seen:
            raise OSError(f"Circular symlink detected at {absolute}")
        seen.add(key)
        if not os.path.islink(absolute):
            return absolute
        target = os.readlink(absolute)
        current = Path(target) if os.path.isabs(target) else Path(absolute).parent / target
    raise OSError(f"Symlink depth exceeded for {path}")


def _variants(file_name):
    base_name = re.sub(r"\.so(?:\.\d+)*$", ".so", file_name)
    values = [file_name, base_name, base_name + ".1", base_name + ".0"]
    return list(dict.fromkeys(values))


def _find_in_zone(zone, variants, deadline):
    for variant in variants:
        direct = os.path.join(zone, variant)
        if os.path.isfile(direct) and os.access(direct, os.R_OK):
            return safe_realpath(direct)
    for root, directories, files in os.walk(zone, followlinks=False):
        _check_deadline(deadline)
        directories[:] = [item for item in directories if item not in {".git", "__pycache__"}]
        for variant in variants:
            if variant in files:
                candidate = os.path.join(root, variant)
                if os.access(candidate, os.R_OK):
                    return safe_realpath(candidate)
    return None


def hunt_missing_library(
    file_name,
    project_dir=".",
    max_retries=1,
    verbose=False,
    deadline=None,
    search_paths=(),
    diagnostics=None,
):
    """Find a readable dependency using project, environment, and custom paths."""
    variants = _variants(os.path.basename(file_name))
    zones = discover_search_paths(project_dir, search_paths)
    for attempt in range(max_retries):
        for zone in zones:
            _check_deadline(deadline)
            if verbose:
                print(f"[RESOLVER] Searching {zone} for {', '.join(variants)}")
            try:
                found = _find_in_zone(zone, variants, deadline)
            except (OSError, PermissionError) as exc:
                message = f"Could not search {zone}: {type(exc).__name__}: {exc}"
                if diagnostics is not None:
                    diagnostics.append(message)
                if verbose:
                    print(f"[RESOLVER] {message}")
                continue
            if found:
                return found
        if attempt + 1 < max_retries:
            _check_deadline(deadline)
            delay = min(0.25, max(0, deadline - time.monotonic())) if deadline else 0.25
            time.sleep(delay)
    return None

