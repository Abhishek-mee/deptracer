"""Configuration and Python-environment search-path discovery."""

from __future__ import annotations

import os
import runpy
import site
import sys
from pathlib import Path


def _site_packages(prefix):
    prefix = Path(prefix)
    candidates = [
        prefix / "Lib" / "site-packages",
        prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages",
    ]
    return [str(path.resolve()) for path in candidates if path.is_dir()]


def load_project_config(project_dir):
    path = Path(project_dir).resolve() / "deptracer_config.py"
    if not path.exists():
        return {}
    values = runpy.run_path(str(path))
    configured = values.get("SEARCH_PATHS", [])
    if not isinstance(configured, (list, tuple)) or not all(
        isinstance(item, (str, os.PathLike)) for item in configured
    ):
        raise ValueError("SEARCH_PATHS in deptracer_config.py must be a list of paths")
    resolved = []
    for item in configured:
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        resolved.append(str(candidate.resolve()))
    return {"search_paths": resolved}


def discover_search_paths(project_dir, extra_paths=()):
    project = Path(project_dir).resolve()
    paths = [
        project,
        project / "lib",
        project / "build" / "lib",
        project / ".venv",
        project / "venv",
    ]

    for variable in ("VIRTUAL_ENV", "CONDA_PREFIX", "PIPENV_ACTIVE"):
        value = os.environ.get(variable)
        if value and variable != "PIPENV_ACTIVE":
            paths.append(Path(value))

    paths.append(Path(sys.prefix))
    try:
        paths.extend(Path(item) for item in site.getsitepackages())
        paths.append(Path(site.getusersitepackages()))
    except Exception:
        pass

    expanded = []
    for path in [*paths, *(Path(item).expanduser() for item in extra_paths)]:
        expanded.append(path)
        expanded.extend(Path(item) for item in _site_packages(path))

    unique = []
    seen = set()
    for path in expanded:
        absolute = str(path.resolve())
        key = os.path.normcase(absolute)
        if key not in seen and os.path.exists(absolute):
            seen.add(key)
            unique.append(absolute)
    return unique

