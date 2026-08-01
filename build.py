"""
build.py

Builds ``dist/LocalLink.exe`` from ``run.py`` using PyInstaller.

The single source of truth for the build is ``LocalLink.spec`` (which
knows about the .env, hidden imports, UPX-disabled setting, etc.).
This file is a thin wrapper that handles the Windows-specific cleanup
(taskkill any running instance, remove the locked .exe) before
invoking PyInstaller with the spec.
"""
import os
import shutil
import subprocess
import sys


def main() -> int:
    print("Building LocalLink executable...")

    # Kill any running instance so Windows releases the file lock.
    # taskkill only exists on Windows; skip on Linux/macOS builds.
    if sys.platform.startswith("win"):
        subprocess.call(
            ["taskkill", "/F", "/IM", "LocalLink.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # Remove the old dist exe so PyInstaller never hits a permission error.
    old_exe = os.path.join("dist", "LocalLink.exe")
    if os.path.exists(old_exe):
        try:
            os.remove(old_exe)
            print("Removed old dist/LocalLink.exe")
        except PermissionError:
            print("ERROR: dist/LocalLink.exe is still locked. Close it and try again.")
            return 1

    # Make sure PyInstaller is available.
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # Use the .spec file as the single source of truth. It already
    # bundles the .env (when present), disables UPX, and lists the
    # hidden imports the engine needs.
    spec_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "LocalLink.spec",
    )
    if not os.path.isfile(spec_path):
        print(f"ERROR: spec file not found: {spec_path}")
        return 1

    command = [sys.executable, "-m", "PyInstaller", "--clean", spec_path]

    print(f"Running: {' '.join(command)}")
    try:
        subprocess.check_call(command)
    except subprocess.CalledProcessError as e:
        print(f"\nBuild FAILED with exit code {e.returncode}")
        return e.returncode

    print("\nBuild complete! Your executable is at: dist/LocalLink.exe")
    print("Run it from PowerShell with:  .\\dist\\LocalLink.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
