#!/usr/bin/env python3
"""One-command installer for the Unsharp bot.

    python3 install.py

Stdlib only, so it runs on a bare Python 3.10+ on Linux, macOS and Windows
without installing anything first.  What it does:

1. checks the Python version,
2. creates a virtual environment in ``.venv`` (skip with ``--no-venv``),
3. installs the dependencies, preferring `uv` when it is available because it
   resolves and installs faster than pip, and falling back to pip otherwise,
4. creates ``config/config.yaml`` and ``.env`` from the templates if missing,
5. prints the exact next commands for your shell.

Re-running it is safe: nothing already in place is overwritten.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

MIN_PYTHON = (3, 10)
ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"

# ANSI colours, disabled when the output is piped or on a dumb terminal.
_TTY = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
BOLD = "\033[1m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""


def step(message: str) -> None:
    print(f"{BOLD}==>{RESET} {message}")


def ok(message: str) -> None:
    print(f"    {GREEN}OK{RESET}  {message}")


def info(message: str) -> None:
    print(f"    {DIM}{message}{RESET}")


def warn(message: str) -> None:
    print(f"    {YELLOW}!{RESET}   {message}")


def fail(message: str) -> None:
    print(f"\n{RED}Installation failed:{RESET} {message}\n", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def check_python() -> None:
    step("Checking Python")
    if sys.version_info < MIN_PYTHON:
        fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required, "
            f"you are running {platform.python_version()}.\n"
            "Install a newer Python from https://www.python.org/downloads/ "
            "and run this script again."
        )
    ok(f"Python {platform.python_version()} on {platform.system()}")


def venv_python(venv_dir: Path) -> Path:
    """Path to the interpreter inside a virtual environment."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def create_venv(venv_dir: Path) -> Path:
    step("Preparing the virtual environment")
    python = venv_python(venv_dir)
    if python.is_file():
        ok(f"reusing {venv_dir.name}{os.sep}")
        return python
    try:
        venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(venv_dir)
    except Exception as exc:
        fail(
            f"could not create the virtual environment ({exc}).\n"
            "On Debian or Ubuntu you may need: sudo apt install python3-venv\n"
            "Or install without a virtual environment: python3 install.py --no-venv"
        )
    if not python.is_file():
        fail(f"the virtual environment looks incomplete: {python} is missing")
    ok(f"created {venv_dir.name}{os.sep}")
    return python


def run(command: list[str], description: str, quiet: bool = True) -> bool:
    """Run a command, returning True on success."""
    info(f"$ {' '.join(str(part) for part in command)}")
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE if quiet else None,
            stderr=subprocess.STDOUT if quiet else None,
            text=True,
            check=False,
        )
    except OSError as exc:
        warn(f"{description} could not start: {exc}")
        return False
    if result.returncode != 0:
        if quiet and result.stdout:
            tail = "\n".join(result.stdout.strip().splitlines()[-12:])
            print(f"{DIM}{tail}{RESET}")
        warn(f"{description} failed (exit code {result.returncode})")
        return False
    return True


# --------------------------------------------------------------------------- #
# Dependency installation
# --------------------------------------------------------------------------- #
def install_dependencies(python: Path, with_dev: bool, editable: bool) -> None:
    """Install the project, preferring `uv` for speed."""
    step("Installing dependencies")
    target = ".[dev]" if with_dev else "."
    uv = shutil.which("uv")

    if uv:
        info("uv detected, using it (faster than pip)")
        command = [uv, "pip", "install", "--python", str(python)]
        command += ["-e", target] if editable else [target]
        if run(command, "uv install"):
            ok("dependencies installed with uv")
            return
        warn("uv failed, falling back to pip")

    # pip: upgrade first so modern wheels and PEP 517 builds work everywhere.
    run([str(python), "-m", "pip", "install", "--upgrade", "pip"], "pip upgrade")
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    command += ["-e", target] if editable else [target]
    if not run(command, "pip install"):
        fail(
            "dependencies could not be installed.\n"
            "Check your internet connection, then run this script again.\n"
            "Behind a proxy, set HTTPS_PROXY before running it."
        )
    ok("dependencies installed with pip")


# --------------------------------------------------------------------------- #
# Configuration bootstrap
# --------------------------------------------------------------------------- #
def bootstrap_files() -> list[str]:
    """Create config.yaml and .env from the templates when they are missing."""
    step("Preparing the configuration files")
    created: list[str] = []
    pairs = [
        (ROOT / "config" / "config.example.yaml", ROOT / "config" / "config.yaml"),
        (ROOT / ".env.example", ROOT / ".env"),
    ]
    for template, target in pairs:
        relative = target.relative_to(ROOT)
        if target.exists():
            ok(f"{relative} already exists, left untouched")
            continue
        if not template.is_file():
            warn(f"template missing: {template.relative_to(ROOT)}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, target)
        if target.name == ".env":
            _restrict_permissions(target)
        ok(f"created {relative}")
        created.append(str(relative))
    return created


def _restrict_permissions(path: Path) -> None:
    """Make the credentials file readable by its owner only (POSIX)."""
    if os.name == "nt":
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass


def verify(python: Path) -> bool:
    step("Verifying the installation")
    command = [
        str(python), "-c",
        "import unsharp_bot, websocket, yaml; print(unsharp_bot.__version__)",
    ]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    except OSError as exc:
        warn(f"verification could not run: {exc}")
        return False
    if result.returncode != 0:
        warn("the package does not import correctly")
        if result.stderr:
            print(f"{DIM}{result.stderr.strip()}{RESET}")
        return False
    ok(f"unsharp-bot {result.stdout.strip()} is importable")
    return True


# --------------------------------------------------------------------------- #
# Final instructions
# --------------------------------------------------------------------------- #
def activation_command(venv_dir: Path) -> str:
    relative = venv_dir.name
    if os.name == "nt":
        shell = os.environ.get("PSModulePath")
        if shell:
            return f".\\{relative}\\Scripts\\Activate.ps1"
        return f"{relative}\\Scripts\\activate.bat"
    return f"source {relative}/bin/activate"


def print_next_steps(used_venv: bool, created: list[str]) -> None:
    print()
    print(f"{GREEN}{BOLD}Installation complete.{RESET}")
    print()
    print(f"{BOLD}Next steps{RESET}")
    number = 1
    if used_venv:
        print(f"  {number}. Activate the environment:")
        print(f"       {BOLD}{activation_command(VENV_DIR)}{RESET}")
        number += 1
    print(f"  {number}. Enter your XTB demo credentials:")
    print(f"       {BOLD}unsharp-bot init{RESET}")
    number += 1
    print(f"  {number}. Check the configuration and the connection:")
    print(f"       {BOLD}unsharp-bot check{RESET}")
    number += 1
    print(f"  {number}. Run without sending any order:")
    print(f"       {BOLD}unsharp-bot run --dry-run{RESET}")
    print()
    if created:
        print(f"{DIM}Created: {', '.join(created)}{RESET}")
    print(
        f"{YELLOW}Reminder:{RESET} start on an XTB demo account. This software is "
        "experimental and is not financial advice."
    )
    print()


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the Unsharp bot in one command.",
    )
    parser.add_argument(
        "--no-venv", action="store_true",
        help="install into the current Python instead of creating .venv",
    )
    parser.add_argument(
        "--dev", action="store_true", help="also install the test dependencies",
    )
    parser.add_argument(
        "--no-editable", action="store_true",
        help="install a normal copy instead of an editable install",
    )
    parser.add_argument(
        "--venv-dir", default=str(VENV_DIR), help="virtual environment location",
    )
    args = parser.parse_args(argv)

    print()
    print(f"{BOLD}Unsharp Candles bot - installer{RESET}")
    print(f"{DIM}{ROOT}{RESET}")
    print()

    check_python()

    if args.no_venv:
        step("Preparing the virtual environment")
        warn("skipped (--no-venv): installing into the current Python")
        python = Path(sys.executable)
    else:
        python = create_venv(Path(args.venv_dir))

    install_dependencies(python, with_dev=args.dev, editable=not args.no_editable)
    created = bootstrap_files()
    verify(python)
    print_next_steps(used_venv=not args.no_venv, created=created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
