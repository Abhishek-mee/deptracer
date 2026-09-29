import os
import subprocess
import time
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest import mock

from deptracer import compiler, core, tracer


class TracerTimeoutTests(unittest.TestCase):
    @mock.patch("deptracer.tracer.os.killpg", create=True)
    @mock.patch("deptracer.tracer.subprocess.Popen")
    @mock.patch("builtins.open", mock.mock_open())
    def test_timeout_kills_the_trace_process_group(self, popen, killpg):
        process = popen.return_value
        process.pid = 4321
        process.communicate.side_effect = [subprocess.TimeoutExpired("strace", 1), ("", "")]

        with self.assertRaises(tracer.TraceTimeoutError):
            tracer.run_and_trace("/project/app", "/project", log_name="/tmp/trace.log", timeout=1)

        killpg.assert_called_once_with(4321, tracer.KILL_SIGNAL)


class CompilerTimeoutTests(unittest.TestCase):
    @mock.patch("deptracer.compiler.custom_progress_bar", return_value=None)
    @mock.patch("deptracer.compiler.os.killpg", create=True)
    @mock.patch("deptracer.compiler.subprocess.Popen")
    @mock.patch("deptracer.compiler.os.path.exists", return_value=True)
    def test_build_timeout_is_reported(self, _exists, popen, killpg, _progress):
        process = popen.return_value
        process.pid = 9876
        process.communicate.side_effect = [subprocess.TimeoutExpired("PyInstaller", 1), (b"", b"")]

        with self.assertRaises(compiler.BuildTimeoutError):
            compiler.build("/project/app.spec", timeout=1)

        killpg.assert_called_once_with(9876, compiler.KILL_SIGNAL)

    @unittest.skipUnless(os.name == "posix", "process-group behavior is Linux-only")
    def test_real_process_group_is_stopped_promptly(self):
        started = time.monotonic()

        with self.assertRaises(compiler.BuildTimeoutError):
            compiler._run_build(
                ["sh", "-c", "sleep 30"],
                project_dir=".",
                timeout=0.1,
                capture_output=True,
            )

        self.assertLess(time.monotonic() - started, 2)


class PipelineBudgetTests(unittest.TestCase):
    def _base_stack(self, stack):
        stack.enter_context(mock.patch("deptracer.core.platform.system", return_value="Linux"))
        stack.enter_context(mock.patch("deptracer.core.Path.exists", return_value=True))
        stack.enter_context(mock.patch("deptracer.core.load_project_config", return_value={}))
        store_class = stack.enter_context(mock.patch("deptracer.core.StateStore"))
        store = store_class.return_value
        store.load.return_value = None
        store.new.return_value = {
            "iteration": 1,
            "injected": [],
            "system_noise": [],
            "history": [],
            "status": "running",
        }
        return store

    @mock.patch("deptracer.core.load_project_config", return_value={})
    @mock.patch("deptracer.core.StateStore")
    @mock.patch("deptracer.core.compiler.build", return_value=True)
    @mock.patch("deptracer.core.fixer.patch_spec_file", return_value={"hidden_imports": 1})
    @mock.patch(
        "deptracer.core.parser.extract_missing_libraries_and_hidden_imports",
        return_value={"binaries": [], "data": [], "hidden_imports": ["plugin"]},
    )
    @mock.patch("deptracer.core.tracer.run_and_trace", return_value=("trace.log", "stderr.log", 1))
    @mock.patch("deptracer.core.Path.exists", return_value=True)
    @mock.patch("deptracer.core.platform.system", return_value="Linux")
    @mock.patch("builtins.open", mock.mock_open())
    def test_iteration_budget_stops_rebuild_loop(
        self,
        _platform,
        _exists,
        _trace,
        _parse,
        _patch,
        _build,
        store_class,
        _config,
    ):
        store = store_class.return_value
        store.load.return_value = None
        store.new.return_value = {
            "iteration": 1,
            "injected": [],
            "system_noise": [],
            "history": [],
            "status": "running",
        }
        result = core.run_akdeptracer(
            "/project",
            "app",
            "app.spec",
            max_iterations=1,
            max_duration=60,
        )

        self.assertFalse(result)
        self.assertTrue(
            any(
                call.args[1:4]
                == ("BUDGET", "failed", "Iteration budget exhausted after 1 iterations.")
                for call in store.event.call_args_list
            )
        )

    def test_overall_budget_expiry_during_trace_is_classified_as_budget(self):
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            store = self._base_stack(stack)
            stack.enter_context(
                mock.patch(
                    "deptracer.core.tracer.run_and_trace",
                    side_effect=tracer.TraceTimeoutError("trace timed out"),
                )
            )
            result = core.run_akdeptracer(
                "/project", "app", "app.spec", trace_timeout=60, max_duration=1
            )
        self.assertFalse(result)
        self.assertTrue(any(call.args[1] == "BUDGET" for call in store.event.call_args_list))

    def test_overall_budget_expiry_during_resolution_is_reported(self):
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            store = self._base_stack(stack)
            stack.enter_context(
                mock.patch("deptracer.core.tracer.run_and_trace", return_value=("trace", "stderr", 1))
            )
            stack.enter_context(
                mock.patch(
                    "deptracer.core.parser.extract_missing_libraries_and_hidden_imports",
                    return_value={"binaries": ["/missing/lib.so"], "data": [], "hidden_imports": []},
                )
            )
            stack.enter_context(
                mock.patch(
                    "deptracer.core._resolve_all",
                    side_effect=core.resolver.ResolutionTimeoutError("resolution budget expired"),
                )
            )
            result = core.run_akdeptracer("/project", "app", "app.spec")
        self.assertFalse(result)
        self.assertTrue(any(call.args[1] == "BUDGET" for call in store.event.call_args_list))

    def test_overall_budget_expiry_during_compilation_is_reported(self):
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            store = self._base_stack(stack)
            stack.enter_context(
                mock.patch("deptracer.core.tracer.run_and_trace", return_value=("trace", "stderr", 1))
            )
            stack.enter_context(
                mock.patch(
                    "deptracer.core.parser.extract_missing_libraries_and_hidden_imports",
                    return_value={"binaries": [], "data": [], "hidden_imports": ["plugin"]},
                )
            )
            stack.enter_context(mock.patch("deptracer.core.fixer.patch_spec_file", return_value={"hidden_imports": 1}))
            stack.enter_context(
                mock.patch(
                    "deptracer.core.compiler.build",
                    side_effect=compiler.BuildTimeoutError("build timed out"),
                )
            )
            result = core.run_akdeptracer(
                "/project", "app", "app.spec", build_timeout=600, max_duration=1
            )
        self.assertFalse(result)
        self.assertTrue(any(call.args[1] == "BUDGET" for call in store.event.call_args_list))

    def test_successful_trace_logs_are_cleaned(self):
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            self._base_stack(stack)
            stack.enter_context(
                mock.patch("deptracer.core.tracer.run_and_trace", return_value=("trace", "stderr", 0))
            )
            stack.enter_context(
                mock.patch(
                    "deptracer.core.parser.extract_missing_libraries_and_hidden_imports",
                    return_value={"binaries": [], "data": [], "hidden_imports": []},
                )
            )
            cleanup = stack.enter_context(mock.patch("deptracer.core._remove_trace_files"))
            result = core.run_akdeptracer("/project", "app", "app.spec")
        self.assertTrue(result)
        cleanup.assert_called_once_with("trace", "stderr")


if __name__ == "__main__":
    unittest.main()
