import io
import json
import os
import tempfile
import unittest
import importlib.util
import platform
import shutil
import threading
import time
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from deptracer import cli, config, core, fixer, native, parser, resolver
from deptracer.licenses import scan_dependencies
from deptracer.state import StateStore


SPEC = """# -*- mode: python -*-
a = Analysis(
    ['app.py'],
    binaries = [
    ],
    datas=
    [
    ],
    hiddenimports = [],
    runtime_hooks=
    [],
)
"""


class SpecFixerTests(unittest.TestCase):
    @mock.patch("deptracer.fixer.os.path.exists", return_value=True)
    def test_multiline_spec_patch_is_valid_and_idempotent(self, _exists):
        payload = {
            "binaries": [("libdemo.so", "/opt/demo/libdemo.so")],
            "data": [("config.json", "/venv/site-packages/demo/config.json")],
            "hidden_imports": ["demo.plugin"],
        }
        patched, changes = fixer.render_spec_patch(SPEC, payload, "/tool/probe.py")
        second, second_changes = fixer.render_spec_patch(patched, payload, "/tool/probe.py")

        self.assertEqual(changes, {"binaries": 1, "data": 1, "hidden_imports": 1, "runtime_hooks": 1})
        self.assertEqual(second, patched)
        self.assertFalse(any(second_changes.values()))
        self.assertIn("'demo'", patched)

    def test_site_packages_destination_preserves_package_path(self):
        self.assertEqual(fixer.get_data_dest("/venv/site-packages/demo/models/model.json"), "demo/models")


class StateTests(unittest.TestCase):
    def test_state_is_project_local_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(directory)
            state = store.new("app", "app.spec", {"dry_run": False})
            store.event(state, "TRACE", "running", "started")
            loaded = store.load()

            self.assertEqual(loaded["history"][0]["message"], "started")
            self.assertEqual(store.path.parent, Path(directory).resolve() / ".deptracer")


class ConfigurationTests(unittest.TestCase):
    def test_project_config_search_paths_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "deptracer_config.py"
            config_file.write_text("SEARCH_PATHS = ['vendor', 'shared']\n", encoding="utf-8")
            loaded = config.load_project_config(directory)
            self.assertEqual(
                loaded["search_paths"],
                [str((Path(directory) / "vendor").resolve()), str((Path(directory) / "shared").resolve())],
            )

    def test_conda_and_project_virtualenv_paths_are_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            conda = project / "conda"
            venv = project / ".venv"
            conda.mkdir()
            venv.mkdir()
            with mock.patch.dict(os.environ, {"CONDA_PREFIX": str(conda)}):
                paths = config.discover_search_paths(project)
            self.assertIn(str(conda.resolve()), paths)
            self.assertIn(str(venv.resolve()), paths)


class ResolverTests(unittest.TestCase):
    def test_custom_search_path_finds_nested_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom = root / "vendor" / "nested"
            custom.mkdir(parents=True)
            dependency = custom / "libdemo.so.1"
            dependency.touch()
            found = resolver.hunt_missing_library(
                "libdemo.so.1",
                root,
                search_paths=[root / "vendor"],
            )
            self.assertEqual(found, str(dependency.resolve()))

    def test_independent_dependencies_are_resolved_in_parallel(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def fake_resolve(file_name, *_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return "/resolved/" + file_name

        dependencies = [("binaries", f"/missing/lib{index}.so") for index in range(4)]
        with mock.patch("deptracer.core.resolver.hunt_missing_library", side_effect=fake_resolve):
            result, diagnostics = core._resolve_all(dependencies, ".", [], None, workers=4, verbose=False)
        self.assertEqual(len(result), 4)
        self.assertEqual(diagnostics, [])
        self.assertGreater(peak, 1)

    def test_search_errors_are_preserved_as_diagnostics(self):
        diagnostics = []
        with mock.patch("deptracer.resolver.discover_search_paths", return_value=["/unreadable"]), mock.patch(
            "deptracer.resolver._find_in_zone", side_effect=PermissionError("denied")
        ):
            found = resolver.hunt_missing_library("libdemo.so", diagnostics=diagnostics)
        self.assertIsNone(found)
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("PermissionError: denied", diagnostics[0])


class NativeBackendTests(unittest.TestCase):
    @mock.patch("deptracer.native._run")
    def test_linux_dependency_output_is_parsed(self, run):
        run.return_value = "libdemo.so => /opt/lib/libdemo.so (0x1)\n/lib64/ld-linux-x86-64.so.2 (0x2)\n"
        self.assertEqual(
            native.inspect_dependencies("/tmp/app", system="linux"),
            ["/opt/lib/libdemo.so", "/lib64/ld-linux-x86-64.so.2"],
        )

    @mock.patch("deptracer.native._run")
    def test_macos_dependency_output_is_parsed(self, run):
        run.return_value = "/tmp/app:\n\t@rpath/libdemo.dylib (compatibility version 1.0.0)\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        self.assertEqual(
            native.inspect_dependencies("/tmp/app", system="darwin"),
            ["@rpath/libdemo.dylib", "/usr/lib/libSystem.B.dylib"],
        )

    def test_windows_system_dlls_are_excluded(self):
        self.assertTrue(native.is_system_dependency("KERNEL32.dll", system="windows"))
        self.assertTrue(native.is_system_dependency("api-ms-win-core-file-l1-1-0.dll", system="windows"))
        self.assertFalse(native.is_system_dependency("custom.dll", system="windows"))

    @mock.patch("deptracer.native.validate_pe_architecture")
    @mock.patch("deptracer.native.platform.system", return_value="Windows")
    def test_windows_dlls_are_planned_adjacent_to_executable(self, _system, _validate):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "app.exe"
            dependency = root / "custom.dll"
            binary.touch()
            dependency.touch()
            result = native.bundle_native(binary, [str(dependency)], root / "output", dry_run=True)
            self.assertEqual(
                Path(result["actions"][0]["destination"]).parent,
                (root / "output").resolve(),
            )

    @unittest.skipIf(
        platform.system() == "Windows" and importlib.util.find_spec("pefile") is None,
        "pefile optional dependency is not installed",
    )
    def test_current_python_native_dependencies_can_be_inspected(self):
        required_tool = {"Linux": "ldd", "Darwin": "otool"}.get(platform.system())
        if required_tool and not shutil.which(required_tool):
            self.skipTest(f"{required_tool} is unavailable")
        self.assertTrue(native.inspect_dependencies(os.sys.executable))


class ParserTests(unittest.TestCase):
    @mock.patch.object(parser.TraceParser, "load_system_cache")
    def test_versioned_shared_library_is_classified_as_binary(self, _cache):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.log"
            trace.write_text(
                '1 openat(AT_FDCWD, "/opt/libdemo.so.3", O_RDONLY) = -1 ENOENT (No such file or directory)\n',
                encoding="utf-8",
            )
            result = parser.extract_missing_libraries_and_hidden_imports(str(trace))
            self.assertEqual(result["binaries"], ["/opt/libdemo.so.3"])

    def test_only_uncaught_traceback_imports_become_hidden_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            stderr = Path(directory) / "stderr.log"
            stderr.write_text(
                "[DEPTRACER-PROBE] No module named '_winapi'\n"
                "ModuleNotFoundError: No module named 'actual_plugin'\n",
                encoding="utf-8",
            )
            self.assertEqual(parser.HiddenImportDetector.detect_hidden_imports(stderr), ["actual_plugin"])


class LicenseTests(unittest.TestCase):
    def test_gpl_evidence_is_reported_for_neighboring_license(self):
        with tempfile.TemporaryDirectory() as directory:
            dependency = Path(directory) / "libdemo.so"
            dependency.write_bytes(b"binary")
            (Path(directory) / "libdemo.so.LICENSE").write_text("GNU GENERAL PUBLIC LICENSE Version 3", encoding="utf-8")
            findings = scan_dependencies([dependency])
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].risk, "gpl")


class CliTests(unittest.TestCase):
    @mock.patch("deptracer.cli.run_akdeptracer", return_value=False)
    def test_build_failure_returns_nonzero(self, _run):
        self.assertEqual(cli.main(["build", ".", "app", "app.spec"]), 1)

    def test_invalid_timeout_is_rejected(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["build", ".", "app", "app.spec", "--trace-timeout", "0"])
        self.assertEqual(raised.exception.code, 2)

    @mock.patch("deptracer.cli.run_akdeptracer", return_value=True)
    def test_json_and_feature_flags_are_forwarded(self, run):
        code = cli.main([
            "build", ".", "app", "app.spec", "--resume", "--dry-run",
            "--output-format", "json", "--search-path", "vendor", "--workers", "2",
            "--license-policy", "error",
        ])
        self.assertEqual(code, 0)
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["resume"])
        self.assertTrue(kwargs["dry_run"])
        self.assertEqual(kwargs["output_format"], "json")
        self.assertEqual(kwargs["search_paths"], ["vendor"])

    @mock.patch("deptracer.cli.bundle_native")
    @mock.patch("deptracer.cli.inspect_dependencies", return_value=["missing.dll"])
    def test_native_unresolved_dependency_returns_nonzero(self, _inspect, bundle):
        bundle.return_value = {
            "binary": "bundle/app.exe",
            "actions": [{"source": "missing.dll", "status": "unresolved"}],
            "licenses": [],
        }
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            binary = Path(directory) / "app.exe"
            binary.touch()
            self.assertEqual(cli.main(["native", str(binary), "--dry-run"]), 1)


class CoreFeatureTests(unittest.TestCase):
    def _stack(self, stack, saved_state=None):
        stack.enter_context(mock.patch("deptracer.core.platform.system", return_value="Linux"))
        stack.enter_context(mock.patch("deptracer.core.Path.exists", return_value=True))
        stack.enter_context(mock.patch("deptracer.core.load_project_config", return_value={}))
        state_class = stack.enter_context(mock.patch("deptracer.core.StateStore"))
        state = state_class.return_value
        state.load.return_value = saved_state
        state.new.return_value = {
            "iteration": 1,
            "injected": [],
            "system_noise": [],
            "history": [],
            "status": "running",
        }
        return state

    def test_dry_run_never_rebuilds_or_writes_spec(self):
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            self._stack(stack)
            stack.enter_context(mock.patch("deptracer.core.tracer.run_and_trace", return_value=("trace", "stderr", 1)))
            stack.enter_context(
                mock.patch(
                    "deptracer.core.parser.extract_missing_libraries_and_hidden_imports",
                    return_value={"binaries": [], "data": [], "hidden_imports": ["plugin"]},
                )
            )
            patch_spec = stack.enter_context(
                mock.patch("deptracer.core.fixer.patch_spec_file", return_value={"hidden_imports": 1})
            )
            build = stack.enter_context(mock.patch("deptracer.core.compiler.build"))
            result = core.run_akdeptracer("/project", "app", "app.spec", dry_run=True)
        self.assertTrue(result)
        self.assertTrue(patch_spec.call_args.kwargs["dry_run"])
        build.assert_not_called()

    def test_resume_uses_saved_iteration_and_can_finish(self):
        saved = {
            "binary_name": "app",
            "spec_name": "app.spec",
            "iteration": 3,
            "injected": ["binaries:/missing/lib.so"],
            "system_noise": [],
            "history": [],
            "status": "failed",
        }
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            state = self._stack(stack, saved)
            stack.enter_context(mock.patch("deptracer.core.tracer.run_and_trace", return_value=("trace", "stderr", 0)))
            stack.enter_context(
                mock.patch(
                    "deptracer.core.parser.extract_missing_libraries_and_hidden_imports",
                    return_value={"binaries": [], "data": [], "hidden_imports": []},
                )
            )
            result = core.run_akdeptracer("/project", "app", "app.spec", resume=True)
        self.assertTrue(result)
        self.assertEqual(saved["iteration"], 3)
        self.assertTrue(any(call.args[1] == "SUCCESS" for call in state.event.call_args_list))


if __name__ == "__main__":
    unittest.main()
