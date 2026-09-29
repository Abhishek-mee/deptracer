"""Idempotent PyInstaller spec-file patching."""

from __future__ import annotations

import ast
import os
import re
import tempfile
from pathlib import Path


class SpecPatchError(RuntimeError):
    pass


def get_data_dest(absolute_path):
    normalized = absolute_path.replace("\\", "/")
    marker = "/site-packages/"
    if marker in normalized:
        relative = normalized.split(marker, 1)[1]
        parent = os.path.dirname(relative)
        return parent.replace("\\", "/") or "."
    parent = os.path.basename(os.path.dirname(absolute_path))
    return parent or "."


def _inject_entries(content, field, entries):
    entries = [entry for entry in entries if entry not in content]
    if not entries:
        return content, 0
    pattern = re.compile(rf"(?<!\.)\b{re.escape(field)}\s*=\s*\[", re.MULTILINE)
    match = pattern.search(content)
    if not match:
        raise SpecPatchError(f"Could not find {field}=[...] in the PyInstaller spec")
    insertion = "\n        " + ",\n        ".join(entries) + ","
    return content[: match.end()] + insertion + content[match.end() :], len(entries)


def render_spec_patch(content, resolved_payload, probe_path=None):
    changes = {"binaries": 0, "data": 0, "hidden_imports": 0, "runtime_hooks": 0}
    binaries = [
        repr((str(Path(source).resolve()), "."))
        for _, source in resolved_payload.get("binaries", [])
        if os.path.exists(source)
    ]
    data = [
        repr((str(Path(source).resolve()), get_data_dest(source)))
        for _, source in resolved_payload.get("data", [])
        if os.path.exists(source)
    ]
    hidden = [repr(module) for module in resolved_payload.get("hidden_imports", [])]
    hooks = [repr(str(Path(probe_path).resolve()))] if probe_path else []

    content, changes["binaries"] = _inject_entries(content, "binaries", binaries)
    content, changes["data"] = _inject_entries(content, "datas", data)
    content, changes["hidden_imports"] = _inject_entries(content, "hiddenimports", hidden)
    content, changes["runtime_hooks"] = _inject_entries(content, "runtime_hooks", hooks)
    ast.parse(content)
    return content, changes


def patch_spec_file(spec_path, resolved_payload, probe_path=None, dry_run=False):
    path = Path(spec_path)
    if not path.exists():
        raise SpecPatchError(f"Spec file does not exist: {path}")
    content = path.read_text(encoding="utf-8")
    patched, changes = render_spec_patch(content, resolved_payload, probe_path)
    if dry_run or patched == content:
        return changes

    fd, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(patched)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return changes
