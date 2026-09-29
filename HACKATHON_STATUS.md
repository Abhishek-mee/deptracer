# deptracer: Hackathon Status and Code Walkthrough

## Current status

The implementation is feature-complete for the submitted hackathon scope and is
validated on Windows and Linux. macOS support is implemented and covered by a
platform-gated integration test, but that test still needs to run on a real macOS
runner (the included GitHub Actions workflow does this after the repository is
pushed).

Local verification on 2026-09-29:

- Windows: 34 tests passed; 4 platform-specific tests skipped.
- Linux/WSL: 34 tests passed; 2 non-Linux tests skipped.
- The Linux test builds a real shared library, loads it dynamically with
  `ctypes`, lets deptracer repair the PyInstaller spec, verifies the library is
  inside the resulting bundle, and runs the binary from a clean directory.
- The native Linux test builds a C executable and shared library, bundles them,
  patches `$ORIGIN/lib`, and runs the relocated executable.
- The Windows test inspects and relocates a real PE executable, then runs it.

## What the code does

The Python/PyInstaller workflow is an iterative repair loop:

1. `cli.py` validates command-line arguments and calls `core.run_akdeptracer`.
2. `compiler.py` creates the initial PyInstaller executable when needed.
3. `tracer.py` runs the executable under `strace` inside Bubblewrap. Trace and
   build processes have timeouts and are terminated as process groups.
4. `parser.py` reads failed file opens and uncaught import errors. It classifies
   shared libraries, data files, and hidden Python imports.
5. `resolver.py` searches project, virtual-environment, Conda, Poetry, Pipenv,
   system, configuration, and CLI-provided paths. Independent lookups run in
   parallel, respect the overall deadline, and preserve search diagnostics.
6. `licenses.py` performs a best-effort GPL/LGPL evidence scan before bundling.
7. `fixer.py` atomically and idempotently adds resolved files/imports to the
   PyInstaller spec and syntax-checks the result.
8. `compiler.py` rebuilds, and the loop traces the new executable again. Success
   is recorded only when the application exits cleanly with no missing runtime
   dependency.
9. `state.py` stores project-local JSON history in `.deptracer/state.json`, which
   powers `--resume` and `deptracer explain`.

`native.py` is the separate native-binary path. It uses `ldd` for ELF, `pefile`
for Windows PE imports, and `otool` for Mach-O. It copies non-system dependencies
and applies the appropriate layout/loader handling (`$ORIGIN/lib`, adjacent
DLLs, or `@loader_path/lib`). The native CLI now returns a failure code when any
dependency remains unresolved.

## Completed challenge items

- Real end-to-end Linux validation with a genuine runtime dependency.
- Trace, build, and overall wall-clock timeouts plus an iteration budget.
- Timeout process-group cleanup and CLI exit-code/boundary tests.
- Project-local state, consistent failure returns, success history, and log
  cleanup.
- Atomic multiline/nonstandard spec patching tests and full-path dependency
  identities in the iterative workflow.
- Resume, dry-run, structured JSON output, and iteration history.
- Custom search paths and Conda/Poetry/Pipenv/virtual-environment discovery.
- Parallel dependency resolution with visible diagnostics.
- Best-effort license compliance warnings/policy.
- C/C++ ELF/RPATH, Windows DLL, and macOS dylib/codesign support.
- Cross-platform GitHub Actions workflow.

## Honest limitations and final submission steps

- Run the macOS integration test on GitHub Actions or a real Mac. Code cannot
  substitute for platform validation.
- The license scanner is a warning heuristic, not legal advice or a complete
  software-composition-analysis database.
- Bubblewrap provides a restricted runtime view; a disposable VM/container is
  still the strongest final release test.
- Native dependency bundling is intentionally conservative. Complex plugin
  loaders and deeply custom transitive loader paths may need explicit
  `--search-path` values and application-specific validation.
- This directory currently has no Git history. Initialize Git, commit, push, and
  let `.github/workflows/deptracer.yml` run before submission.

For a hackathon demo, show the real Linux `ctypes` test and the generated
`.deptracer/state.json`. Those two artifacts demonstrate that the project repairs
and verifies a binary rather than merely printing a success message.
