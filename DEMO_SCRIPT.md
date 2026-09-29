# Three-minute demo script

## 1. Start with the problem

“This application loads a C shared library at runtime using `ctypes`. PyInstaller
does not see that dependency during static analysis, so the first executable is
incomplete even though it was built successfully.”

## 2. Run the real integration test

From Windows with WSL:

```powershell
wsl.exe bash -lc "cd /mnt/d/deptracer-main && /tmp/deptracer-venv/bin/python -m unittest tests.integration.test_e2e_linux.LinuxEndToEndTests.test_runtime_ctypes_dependency_runs_from_clean_directory -v"
```

Point out these lines as they appear:

```text
[CORE] Starting iteration 1
[RESOLVER] Resolved /usr/lib/libdeptracer_answer.so
[FIXER] Spec file patched.
[COMPILER] Build Successful!
[CORE] Starting iteration 2
[CORE] Executable completed cleanly in the isolation sandbox.
```

## 3. Explain why the test matters

“This is not a mocked dependency. The test compiles a real `.so`, builds a real
PyInstaller executable, confirms that deptracer inserted the library into the
archive, moves the binary into a clean directory, and checks that it prints the
expected value, `42`.”

## 4. Show reliability features

Open `deptracer/core.py` and briefly identify:

- Trace and build timeouts.
- The overall deadline and iteration limit.
- Parallel dependency resolution.
- The success condition after a clean verification run.

Then show `.deptracer/state.json` or run:

```bash
deptracer explain /path/to/project
```

“Every iteration is recorded, so a failed run still explains where it stopped
and can be resumed.”

## 5. Close

“The important difference is that deptracer does not call a binary production
ready because a patch command completed. It rebuilds and runs the application
again. The final result is evidence, not optimism.”

