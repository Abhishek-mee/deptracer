import subprocess
import os
import signal
import tempfile

KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


class TraceTimeoutError(TimeoutError):
    """Raised when the traced application exceeds its execution budget."""


class TraceLaunchError(RuntimeError):
    """Raised when strace or Bubblewrap cannot be launched."""


def _kill_process_tree(process):
    """Stop strace and every process it spawned."""
    try:
        os.killpg(process.pid, KILL_SIGNAL)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        process.kill()

def run_and_trace(
    executable_path,
    project_dir=".",
    log_name=None,
    verbose=False,
    timeout=60,
):
    owns_logs = log_name is None
    if log_name is None:
        log_fd, log_name = tempfile.mkstemp(prefix="deptracer-", suffix=".trace.log")
        os.close(log_fd)
        stderr_fd, stderr_log = tempfile.mkstemp(prefix="deptracer-", suffix=".stderr.log")
        os.close(stderr_fd)
    else:
        log_name = os.path.abspath(log_name)
        stderr_log = log_name + ".stderr"

    abs_executable = os.path.abspath(executable_path)
    abs_project = os.path.abspath(project_dir)
    
    bwrap_jail = [
        "bwrap",
        "--dev-bind", "/", "/",
        "--tmpfs", "/home",
        "--ro-bind", abs_project, abs_project,
        "--bind", abs_executable, abs_executable,
        "--chdir", abs_project,
        "--unshare-all",
        abs_executable
    ]
    
    command = [
        "strace",
        "-f",
        "-s", "2048",
        "-o", log_name,
        "-e", "trace=file"
    ] + bwrap_jail
    
    try:
        with open(stderr_log, 'w') as err_file:
            process = subprocess.Popen(
                command,
                cwd=project_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err_file,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _kill_process_tree(process)
                process.communicate()
                raise TraceTimeoutError(
                    f"Traced application exceeded the {timeout:g}s timeout"
                ) from exc
            rc = process.returncode

        # Bubblewrap failures otherwise look like ordinary application exits,
        # which hides the actual environment/setup problem from the user.
        if rc:
            try:
                launcher_error = open(stderr_log, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                launcher_error = ""
            if "bwrap:" in launcher_error:
                detail = launcher_error.strip().splitlines()[-1]
                raise TraceLaunchError(f"Bubblewrap could not create the sandbox: {detail}")
            
        return log_name, stderr_log, rc
        
    except TraceTimeoutError:
        if owns_logs:
            for path in (log_name, stderr_log):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        raise
    except Exception as exc:
        if owns_logs:
            for path in (log_name, stderr_log):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        raise TraceLaunchError(f"Could not launch strace/Bubblewrap: {exc}") from exc
