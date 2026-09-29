import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from deptracer.core import run_akdeptracer


REQUIRED_TOOLS = ("gcc", "strace", "bwrap")


@unittest.skipUnless(platform.system() == "Linux", "Linux integration test")
@unittest.skipUnless(all(shutil.which(tool) for tool in REQUIRED_TOOLS), "requires gcc, strace, and bwrap")
class LinuxEndToEndTests(unittest.TestCase):
    def test_runtime_ctypes_dependency_runs_from_clean_directory(self):
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            self.skipTest("PyInstaller is not installed")

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            vendor = project / "vendor"
            clean = Path(directory) / "clean"
            vendor.mkdir(parents=True)
            clean.mkdir()
            (project / "answer.c").write_text(
                "int deptracer_answer(void) { return 42; }\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "answer.c", "-o", "vendor/libdeptracer_answer.so"],
                cwd=project,
                check=True,
            )
            (project / "app.py").write_text(
                "import ctypes\n"
                "library = ctypes.CDLL('libdeptracer_answer.so')\n"
                "library.deptracer_answer.restype = ctypes.c_int\n"
                "print(library.deptracer_answer())\n",
                encoding="utf-8",
            )
            subprocess.run(
                [os.sys.executable, "-m", "PyInstaller", "--onefile", "--name", "app", "app.py"],
                cwd=project,
                check=True,
                capture_output=True,
            )

            succeeded = run_akdeptracer(
                project,
                "app",
                "app.spec",
                trace_timeout=20,
                build_timeout=180,
                max_iterations=3,
                max_duration=300,
                search_paths=[vendor],
                license_policy="ignore",
            )
            self.assertTrue(succeeded)

            final_binary = project / "dist" / "app"
            archive_viewer = Path(os.sys.executable).with_name("pyi-archive_viewer")
            archive = subprocess.run(
                [archive_viewer, "-l", final_binary],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("libdeptracer_answer.so", archive.stdout)
            isolated_binary = clean / "app"
            shutil.copy2(final_binary, isolated_binary)
            result = subprocess.run(
                [isolated_binary],
                cwd=clean,
                env={"PATH": "/usr/bin:/bin", "HOME": str(clean)},
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "42")

    def test_native_elf_bundle_runs_with_origin_rpath(self):
        from deptracer.native import bundle_native, inspect_dependencies

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "vendor"
            output = root / "bundle"
            vendor.mkdir()
            (root / "library.c").write_text("int native_answer(void) { return 7; }\n", encoding="utf-8")
            (root / "main.c").write_text(
                "#include <stdio.h>\nint native_answer(void);\nint main(void) { printf(\"%d\\n\", native_answer()); return 0; }\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "library.c", "-Wl,-soname,libdeptracer_native.so", "-o", "vendor/libdeptracer_native.so"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["gcc", "main.c", "-Lvendor", "-ldeptracer_native", "-Wl,-rpath,$ORIGIN/vendor", "-o", "native-app"],
                cwd=root,
                check=True,
            )

            dependencies = inspect_dependencies(root / "native-app")
            self.assertTrue(any(path.endswith("libdeptracer_native.so") for path in dependencies))
            bundled = bundle_native(root / "native-app", dependencies, output)
            result = subprocess.run(
                [bundled["binary"]],
                cwd=output,
                env={"PATH": "/usr/bin:/bin", "HOME": str(output)},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "7")


if __name__ == "__main__":
    unittest.main()
