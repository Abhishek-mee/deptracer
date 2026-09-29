# deptracer

## The short version

PyInstaller is good at bundling dependencies it can see. The difficult bugs are
the dependencies an application loads only while it is running: a shared library
opened with `ctypes`, a plugin imported by name, or a model loaded from a path.
Those files can exist on the developer's laptop and quietly hide the problem.
The packaged application then reaches a clean machine and crashes.

deptracer finds those runtime dependencies by running the packaged application
in a restricted Linux environment and watching the files it tries to open. When
it finds something missing, it searches for the real file, adds it to the
PyInstaller spec, rebuilds the application, and tests the result again. It stops
only when the binary runs cleanly or when a clear time, iteration, or resolution
failure is reached.

## Why we built it

The frustrating part of a broken PyInstaller build is that the error often
appears somewhere other than the machine that produced it. Manually adding one
file at a time to a spec works, but runtime dependency chains can be several
levels deep. We wanted the debugging loop to be repeatable and observable—not a
sequence of guesses followed by a reassuring but unverified success message.

## What we built

- A `strace` and Bubblewrap based runtime tracer for Linux PyInstaller builds.
- An iterative repair loop that edits the spec, rebuilds, and verifies again.
- Detection for shared libraries, runtime data files, and hidden Python imports.
- Trace, compilation, wall-clock, and iteration limits with process cleanup.
- Resume support and project-local JSON history for interrupted runs.
- Dry-run and JSON output for CI and automation.
- Parallel resolution across project, virtualenv, Conda, Poetry, Pipenv, system,
  configured, and command-line search paths.
- Best-effort GPL/LGPL evidence warnings before files are bundled.
- Native dependency inspection and bundling for Linux ELF, Windows PE/DLL, and
  macOS Mach-O/dylib applications.
- A cross-platform GitHub Actions workflow.

## How it works

```text
Build application
       |
       v
Run under strace + Bubblewrap
       |
       v
Missing files or imports?
   | no              | yes
   v                 v
Success       Resolve real files
                     |
                     v
             Patch PyInstaller spec
                     |
                     v
                  Rebuild
                     |
                     +----> trace again
```

Every iteration is saved to `.deptracer/state.json`. `deptracer explain` turns
that history into a readable diagnosis, so a failed run still tells the user
what happened and what to try next.

## Proof that it works

We tested the main claim with a real runtime-only dependency—not a mocked file.
The integration test:

1. Compiles a C shared library that returns the value `42`.
2. Builds a Python application that loads the library by name with `ctypes`.
3. Creates a PyInstaller executable that initially lacks the library.
4. Runs deptracer and lets it discover and repair the missing dependency.
5. Inspects the finished PyInstaller archive to confirm the `.so` is present.
6. Copies the executable to a clean directory and runs it with a minimal
   environment.
7. Confirms that it exits successfully and prints `42`.

Latest local results:

- Windows: 34 tests passed; four platform-specific tests skipped.
- Linux: 34 tests passed; the two non-Linux integrations skipped.
- Real Linux PyInstaller runtime-dependency test: passed.
- Real Linux C/ELF relocation test: passed.
- Real Windows PE relocation test: passed.

## What was difficult

Timeouts were more subtle than adding a timer. Killing only the parent process
could leave `strace`, Bubblewrap, PyInstaller, or the application alive. We had
to manage process groups and test that they actually disappeared.

Spec editing was another risky area. PyInstaller specs are Python programs, not
simple configuration files. The patcher therefore makes atomic, idempotent
changes and parses the result before replacing the original file.

We also learned that “the build command succeeded” is not enough. Our final
success condition is a clean verification run, and the integration test checks
the contents of the produced bundle as well.

## Honest limitations

- The macOS backend is implemented and has a platform-gated integration test,
  but it still needs confirmation on a real Mac or the included macOS CI runner.
- License detection is a useful warning heuristic, not legal advice or a full
  software-composition-analysis system.
- Bubblewrap provides a restricted filesystem view, but a disposable VM or
  container remains the strongest final release test.
- Applications with custom plugin loaders or unusual transitive native-library
  layouts may still require explicit search paths.

## Running it

```bash
pip install -e ".[build]"

# Build, trace, repair, and verify
deptracer build /path/to/project app app.spec

# Preview changes without editing or rebuilding
deptracer build /path/to/project app app.spec --dry-run

# Explain the latest run
deptracer explain /path/to/project
```

Linux requires `strace` and Bubblewrap. Native ELF relocation also uses
`patchelf`. Windows PE inspection uses the optional `pefile` dependency.

## What comes next

The next step is to test against a wider set of real applications and make the
native transitive-dependency model more sophisticated. A longer-term version
could also expose the iteration history as a CI artifact and support additional
runtime ecosystems such as Java.

## One-sentence pitch

deptracer turns “works on my machine” PyInstaller failures into a traceable,
repeatable repair loop—and verifies the binary before calling it fixed.

