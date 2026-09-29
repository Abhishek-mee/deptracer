"""Static native dependency inspection and local bundling backends."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from .licenses import scan_dependencies


class NativeDependencyError(RuntimeError):
    pass


WINDOWS_SYSTEM_DLLS = {
    "advapi32.dll", "bcrypt.dll", "comdlg32.dll", "crypt32.dll", "gdi32.dll",
    "kernel32.dll", "ntdll.dll", "ole32.dll", "oleaut32.dll", "rpcrt4.dll",
    "shell32.dll", "user32.dll", "ws2_32.dll",
}


def is_system_dependency(path, system=None):
    system = (system or platform.system()).lower()
    normalized = str(path).replace("\\", "/")
    name = os.path.basename(normalized).lower()
    if system == "windows":
        return name in WINDOWS_SYSTEM_DLLS or name.startswith(("api-ms-win-", "ext-ms-win-"))
    if system == "darwin":
        return normalized.startswith(("/System/", "/usr/lib/"))
    return bool(re.match(r"^(?:ld-linux|libc|libdl|libm|libpthread|librt)\b", name)) and normalized.startswith(("/lib", "/usr/lib"))


def _run(command):
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise NativeDependencyError(f"Could not run {' '.join(command)}: {exc}") from exc
    return result.stdout


def inspect_dependencies(binary_path, system=None):
    binary = str(Path(binary_path).resolve())
    system = (system or platform.system()).lower()
    if system == "linux":
        output = _run(["ldd", binary])
        dependencies = []
        for line in output.splitlines():
            match = re.search(r"=>\s+(/\S+)", line)
            direct = re.match(r"\s*(/\S+)", line)
            if match or direct:
                dependencies.append((match or direct).group(1))
        return dependencies
    if system == "darwin":
        output = _run(["otool", "-L", binary])
        return [
            line.strip().split(" ", 1)[0]
            for line in output.splitlines()[1:]
            if line.strip()
        ]
    if system == "windows":
        try:
            import pefile
        except ImportError as exc:
            raise NativeDependencyError(
                "Windows PE inspection requires the optional 'pefile' package"
            ) from exc
        pe = pefile.PE(binary)
        return [
            entry.dll.decode(errors="replace")
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
        ]
    raise NativeDependencyError(f"Unsupported platform: {system}")


def validate_pe_architecture(binary_path, dependency_paths):
    try:
        import pefile
    except ImportError as exc:
        raise NativeDependencyError("Windows architecture validation requires 'pefile'") from exc
    expected = pefile.PE(str(binary_path), fast_load=True).FILE_HEADER.Machine
    for dependency in dependency_paths:
        if not os.path.exists(dependency):
            continue
        actual = pefile.PE(str(dependency), fast_load=True).FILE_HEADER.Machine
        if actual != expected:
            raise NativeDependencyError(
                f"Architecture mismatch: {dependency} uses PE machine 0x{actual:04x}; "
                f"target uses 0x{expected:04x}"
            )


def bundle_native(binary_path, dependencies, output_dir, dry_run=False, codesign=False):
    binary = Path(binary_path).resolve()
    output = Path(output_dir).resolve()
    system = platform.system().lower()
    lib_dir = output if system == "windows" else output / "lib"
    actions = []
    target_binary = output / binary.name
    normalized_dependencies = [
        dependency if isinstance(dependency, tuple) else (dependency, dependency)
        for dependency in dependencies
    ]
    concrete_dependencies = [
        resolved for _, resolved in normalized_dependencies
        if os.path.isabs(resolved) and os.path.exists(resolved)
    ]
    if system == "windows":
        validate_pe_architecture(binary, concrete_dependencies)
    if not dry_run:
        output.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target_binary)
    for reference, dependency in normalized_dependencies:
        if is_system_dependency(reference) or is_system_dependency(dependency):
            actions.append({"source": dependency, "reference": reference, "status": "system-skipped"})
            continue
        source = Path(dependency)
        if not source.is_absolute() or not source.exists():
            actions.append({"source": dependency, "reference": reference, "status": "unresolved"})
            continue
        destination = lib_dir / source.name
        actions.append({"source": str(source), "reference": reference, "destination": str(destination), "status": "planned" if dry_run else "copied"})
        if not dry_run:
            lib_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    if not dry_run:
        if system == "linux" and shutil.which("patchelf"):
            _run(["patchelf", "--set-rpath", "$ORIGIN/lib", str(target_binary)])
        elif system == "darwin":
            _run(["install_name_tool", "-add_rpath", "@loader_path/lib", str(target_binary)])
            for action in actions:
                if action["status"] == "copied":
                    _run([
                        "install_name_tool",
                        "-change",
                        action["reference"],
                        "@rpath/" + Path(action["destination"]).name,
                        str(target_binary),
                    ])
            if codesign:
                for action in actions:
                    if action["status"] == "copied":
                        _run(["codesign", "--force", "--sign", "-", action["destination"]])
                _run(["codesign", "--force", "--sign", "-", str(target_binary)])
    return {
        "binary": str(target_binary),
        "actions": actions,
        "licenses": [item.as_dict() for item in scan_dependencies(concrete_dependencies) if os.path.exists(item.path)],
    }
