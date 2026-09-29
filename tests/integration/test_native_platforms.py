import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from deptracer.native import bundle_native, inspect_dependencies


@unittest.skipUnless(platform.system() == "Windows", "Windows integration test")
class WindowsNativeIntegrationTests(unittest.TestCase):
    def test_windows_system_executable_bundle_runs(self):
        source = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
        with tempfile.TemporaryDirectory() as directory:
            dependencies = inspect_dependencies(source)
            bundled = bundle_native(source, dependencies, Path(directory) / "bundle")
            result = subprocess.run(
                [bundled["binary"], "where.exe"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("where.exe", result.stdout.lower())


@unittest.skipUnless(platform.system() == "Darwin", "macOS integration test")
class MacNativeIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("clang") and shutil.which("otool"), "requires clang and otool")
    def test_macho_dylib_bundle_runs_with_loader_rpath(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "vendor"
            output = root / "bundle"
            vendor.mkdir()
            (root / "library.c").write_text("int native_answer(void) { return 9; }\n", encoding="utf-8")
            (root / "main.c").write_text(
                "#include <stdio.h>\nint native_answer(void);\nint main(void) { printf(\"%d\\n\", native_answer()); return 0; }\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["clang", "-dynamiclib", "library.c", "-install_name", "@rpath/libdeptracer_native.dylib", "-o", "vendor/libdeptracer_native.dylib"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["clang", "main.c", "-Lvendor", "-ldeptracer_native", "-Wl,-rpath,@loader_path/vendor", "-o", "native-app"],
                cwd=root,
                check=True,
            )
            references = inspect_dependencies(root / "native-app")
            custom_reference = next(item for item in references if item.endswith("libdeptracer_native.dylib"))
            dependencies = [
                (item, str(vendor / "libdeptracer_native.dylib")) if item == custom_reference else item
                for item in references
            ]
            bundled = bundle_native(root / "native-app", dependencies, output, codesign=True)
            result = subprocess.run(
                [bundled["binary"]],
                cwd=output,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "9")


if __name__ == "__main__":
    unittest.main()
