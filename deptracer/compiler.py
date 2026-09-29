# linux_core/compiler.py
import subprocess
import os
import sys
import time
import threading
import signal

KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


class BuildTimeoutError(TimeoutError):
    """Raised when PyInstaller exceeds its build budget."""


def _kill_process_tree(process):
    """Stop PyInstaller and every process it spawned."""
    try:
        os.killpg(process.pid, KILL_SIGNAL)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        process.kill()


def _run_build(command, project_dir, timeout, capture_output):
    process = subprocess.Popen(
        command,
        cwd=project_dir,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(process)
        process.communicate()
        raise BuildTimeoutError(
            f"PyInstaller exceeded the {timeout:g}s timeout"
        ) from exc

    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return stdout, stderr

def custom_progress_bar(stop_event):
    """Animates a pure-Python progress bar on a loop without external packages."""
    animation_chars = ["■", "■", "■", "■", "■", "■", "■", "■", "■", "■"]
    i = 0
    while not stop_event.is_set():
        fill_level = (i % 10) + 1
        bar = "█" * fill_level + "░" * (10 - fill_level)
        sys.stdout.write(f"\r\033[93m[COMPILER] Building via PyInstaller [{bar}] Compiling system blocks...\033[0m")
        sys.stdout.flush()
        time.sleep(0.25)
        i += 1
        
    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()

def build(spec_path, iteration=1, verbose=False, timeout=600, quiet=False):
    if not os.path.exists(spec_path):
        return False

    project_dir = os.path.dirname(os.path.abspath(spec_path))
    spec_filename = os.path.basename(spec_path)
    
    if iteration == 1:
        command = [sys.executable, "-m", "PyInstaller", "--clean", "-y", spec_filename]
    else:
        command = [sys.executable, "-m", "PyInstaller", "-y", spec_filename]
    
    if verbose and not quiet:
        print("\n\033[93m[COMPILER-VERBOSE] Streaming raw PyInstaller output...\033[0m")
        try:
            _run_build(command, project_dir, timeout, capture_output=False)
            print("\033[92m [COMPILER] Build Successful! New clean binary deployed to dist/ folder.\033[0m")
            return True
        except subprocess.CalledProcessError as e:
            print("\n\033[91m\033[1m [COMPILER] PyInstaller Compilation Fatal Crash!\033[0m")
            return False
        except BuildTimeoutError:
            raise
        except Exception as e:
            print(f"\n\033[91m [COMPILER] Unexpected engine failure: {e}\033[0m")
            return False

    stop_compiler_animation = threading.Event()
    progress_thread = threading.Thread(target=custom_progress_bar, args=(stop_compiler_animation,))

    try:
        if not quiet:
            progress_thread.start()

        _, stderr = _run_build(command, project_dir, timeout, capture_output=True)

        stop_compiler_animation.set()
        if progress_thread.is_alive():
            progress_thread.join()
        
        if not quiet:
            print("\033[92m [COMPILER] Build Successful! New clean binary deployed to dist/ folder.\033[0m")
        return True

    except subprocess.CalledProcessError as e:
        stop_compiler_animation.set()
        if progress_thread.is_alive():
            progress_thread.join()
        if not quiet:
            print("\n\033[91m\033[1m [COMPILER] PyInstaller Compilation Fatal Crash!\033[0m")
            if e.stderr:
                print(e.stderr.decode(errors="replace").strip())
        return False
    except BuildTimeoutError:
        raise
    except Exception as e:
        stop_compiler_animation.set()
        progress_thread.join()
        if not quiet:
            print(f"\n\033[91m [COMPILER] Unexpected engine failure: {e}\033[0m")
        return False
    finally:
        if progress_thread.is_alive():
            stop_compiler_animation.set()
            progress_thread.join()
