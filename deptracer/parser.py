import re
import os
import subprocess
import time
from pathlib import Path


class ParseTimeoutError(TimeoutError):
    pass

class HiddenImportDetector:
    PATTERN = re.compile(
        r'(?:ModuleNotFoundError|ImportError):\s*No module named [\'"]([a-zA-Z0-9_\.]+)[\'"]'
    )
    
    @classmethod
    def detect_hidden_imports(cls, stderr_log_path, deadline=None):
        hidden = set()
        if not stderr_log_path or not os.path.exists(stderr_log_path):
            return []
        try:
            with open(stderr_log_path, 'r', errors='ignore') as f:
                for line_number, line in enumerate(f, 1):
                    if deadline is not None and line_number % 1024 == 0 and time.monotonic() >= deadline:
                        raise ParseTimeoutError("Overall time budget expired while parsing stderr")
                    m1 = cls.PATTERN.search(line)
                    if m1: hidden.add(m1.group(1))
                    
        except ParseTimeoutError:
            raise
        except OSError:
            pass
        return list(hidden)

class TraceParser:
    _SYSTEM_CACHE = set()
    
    SYSCALL_ANY = re.compile(
        r'(?:openat|newfstatat|stat|access)\((?:AT_FDCWD,\s*)?' 
        r'"([^"\\]*(?:\\.[^"\\]*)*)"' 
        r'[^)]*\)\s*=\s*(-1\s+\w+|\d+)'
    )
    
    TRACKED_EXTENSIONS = {
    '.so', '.dll', '.dylib', '.pyd',
    '.pt', '.pth', '.onnx', '.pb', '.h5', '.joblib',
    '.npy', '.npz', '.pickle', '.pkl', '.json', '.yaml', '.yml', '.toml', '.csv',
    '.wav', '.mp3', '.flac', '.jpg', '.png', '.gif',
    '.txt', '.md', '.db', '.sqlite', '.dat'
    }
    RELEVANT_ERRORS = {'ENOENT', 'EACCES', 'EPERM', 'ENOEXEC'}

    @classmethod
    def load_system_cache(cls):
        if cls._SYSTEM_CACHE: return
        try:
            result = subprocess.run(["ldconfig", "-p"], stdout=subprocess.PIPE, text=True)
            for line in result.stdout.splitlines():
                if "=>" in line:
                    lib_name = line.split("=>")[0].strip().split()[0]
                    cls._SYSTEM_CACHE.add(lib_name)
        except (OSError, subprocess.SubprocessError):
            return

    @classmethod
    def is_system_noise(cls, file_path):
        file_name = os.path.basename(file_path)
        if file_name.startswith('_') or file_name.startswith('libpython'): 
            return True
        base_lib_name = re.sub(r'\.so(\.\d+)*$', '.so', file_name)
        if base_lib_name in cls._SYSTEM_CACHE or file_name in cls._SYSTEM_CACHE: 
            return True
        if 'glibc-hwcaps' in file_path: 
            return True
        if 'lib-dynload' in file_path and '.cpython-' in file_name: 
            return True
        return False

    @staticmethod
    def _is_binary_file(path: str) -> bool:
        for ext in TraceParser.TRACKED_EXTENSIONS:
            if ext in path:
                idx = path.rfind(ext)
                after_ext = path[idx + len(ext):]
                if after_ext == '' or re.match(r'^(\.\d+)*$', after_ext):
                    return True
        return False

    @classmethod
    def stream_missing_libraries(cls, log_file_path, verbose=False, deadline=None):
        cls.load_system_cache()
        
        library_status = {}  
        path_mapping = {}    
        seen_warnings = set()

        with open(log_file_path, "r") as file:
            for line_num, line in enumerate(file, 1):
                if deadline is not None and line_num % 1024 == 0 and time.monotonic() >= deadline:
                    raise ParseTimeoutError("Overall time budget expired while parsing strace output")
                match = cls.SYSCALL_ANY.search(line)
                if not match: continue

                path = match.group(1)
                result_value = match.group(2)

                if not cls._is_binary_file(path): 
                    continue

                filename = os.path.basename(path)

                if not result_value.startswith("-1"): 
                    if "/tmp/_MEI" in path or path.startswith("/lib") or path.startswith("/usr/lib"):
                        library_status[filename] = "FOUND"
                    continue

                parts = result_value.split()
                if len(parts) < 2: continue
                errno = parts[1]

                if errno not in cls.RELEVANT_ERRORS: 
                    continue

                if '...' in path:
                    if path not in seen_warnings:
                        if verbose:
                            print(f"[WARNING] Line {line_num}: Path truncated by strace: {path}")
                        seen_warnings.add(path)
                    continue

                if errno in {'EACCES', 'EPERM'}:
                    if path not in seen_warnings:
                        if verbose:
                            print(f"[WARNING] Line {line_num}: File locked/unreadable ({errno}): {path}")
                        seen_warnings.add(path)
                    continue

                if errno == 'ENOENT':
                    if not cls.is_system_noise(path):
                        if library_status.get(filename) != "FOUND":
                            library_status[filename] = "MISSING"
                            path_mapping[filename] = path
                    else:
                        if verbose:
                            print(f"    \033[90m[PARSER-VERBOSE] Filtered system cache hit: {path}\033[0m")

        for filename, status in library_status.items():
            if status == "MISSING":
                yield path_mapping[filename]

def extract_missing_libraries(log_file_path, verbose=False, deadline=None):
    yield from TraceParser.stream_missing_libraries(log_file_path, verbose=verbose, deadline=deadline)

def extract_missing_libraries_and_hidden_imports(log_file_path, stderr_log_path=None, verbose=False, deadline=None):
    raw_missing = list(extract_missing_libraries(log_file_path, verbose=verbose, deadline=deadline))

    hidden_modules = []
    if stderr_log_path:
        hidden_modules = HiddenImportDetector.detect_hidden_imports(stderr_log_path, deadline=deadline)

    binaries = []
    data_files = []

    for item in raw_missing:
        if re.search(r'(?:\.so(?:\.\d+)*|\.dll|\.dylib|\.pyd)$', item, re.IGNORECASE):
            binaries.append(item)
        elif item.endswith(('.pt', '.pth', '.onnx', '.pb', '.h5', '.joblib', '.npy', '.npz', '.pickle', '.pkl', '.json', '.yaml', '.yml', '.toml', '.csv', '.wav', '.mp3', '.flac', '.jpg', '.png', '.gif', '.txt', '.md', '.db', '.sqlite', '.dat')):
            data_files.append(item)

    return {
        'binaries': binaries,
        'data': data_files,
        'hidden_imports': hidden_modules
    }
