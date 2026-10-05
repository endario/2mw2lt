"""The macOS agent job's launcher bundle (doc 141): built on the host from `host/darwin/launcher.c`
once, and after that left byte for byte, because its ad hoc signature is what the owner's privacy
grant is keyed to."""
from __future__ import annotations

import hashlib
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

BUNDLE = "2mw2lt agent.app"
IDENTIFIER = "com.2mw2lt.agent"
EXECUTABLE = "2mw2lt-agent"
DIGEST_KEY = "2mw2ltLauncherDigest"
# Within a release: the launcher is built from the release a job runs, and from nowhere else, so an
# installer run from another copy of this tree cannot rebuild it and void the grant.
SOURCE = Path("steering") / "host" / "darwin" / "launcher.c"
Run = Callable[..., subprocess.CompletedProcess[str]]


def _info() -> dict:
    return {"CFBundleExecutable": EXECUTABLE, "CFBundleIdentifier": IDENTIFIER,
            "CFBundleName": "2mw2lt agent", "CFBundlePackageType": "APPL", "LSBackgroundOnly": True}


def digest(source: Path) -> str:
    """What the bundle is built from. A change to it is the only thing that rebuilds one."""
    return hashlib.sha256(source.read_bytes() + plistlib.dumps(_info(), sort_keys=True)).hexdigest()


def executable(bundle: Path) -> Path:
    return bundle / "Contents" / "MacOS" / EXECUTABLE


def _built_from(bundle: Path) -> str | None:
    try:
        return plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes()).get(DIGEST_KEY)
    except (OSError, plistlib.InvalidFileException, AttributeError):
        return None


def _sound(bundle: Path, run: Run) -> bool:
    # Absolute, always: `codesign` reads an argument that begins with digits as a pid (doc 141 §1).
    return (executable(bundle).is_file()
            and run(["codesign", "--verify", "--strict", str(bundle.resolve())],
                    capture_output=True, text=True).returncode == 0)


def _sdks(run: Run) -> list[str | None]:
    """The compiler's own SDK first, then every other one beside it, newest first: one host's
    Command Line Tools default to an SDK their own linker cannot read (doc 141 §1). A workaround
    for that defect, to go with it; whichever SDK links stays in the bundle for its life."""
    found = run(["xcrun", "--show-sdk-path"], capture_output=True, text=True)
    if found.returncode:
        return [None]
    default = Path(found.stdout.strip())
    def version(path: Path) -> tuple[int, ...]:
        return tuple(int(n) for n in re.findall(r"\d+", path.name))
    others = sorted((p for p in default.parent.glob("MacOSX*.sdk")
                     if not p.is_symlink() and p.resolve() != default.resolve()), key=version, reverse=True)
    return [None, *(str(p) for p in others)]


def _build(bundle: Path, source: Path, want: str, run: Run) -> None:
    staging = bundle.with_name(bundle.name + ".next")
    shutil.rmtree(staging, ignore_errors=True)
    binary = executable(staging)
    binary.parent.mkdir(parents=True)
    (staging / "Contents" / "Info.plist").write_bytes(plistlib.dumps({**_info(), DIGEST_KEY: want}, sort_keys=True))
    errors = []
    for sdk in _sdks(run):
        compiled = run(["cc", "-O2", *(["-isysroot", sdk] if sdk else []), "-o", str(binary), str(source)],
                       capture_output=True, text=True)
        if compiled.returncode == 0:
            break
        errors.append(compiled.stderr.strip().splitlines()[-1:] or ["cc failed"])
    else:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"cc could not build it with any SDK: {errors[0][0]}")
    signed = run(["codesign", "--sign", "-", "--force", str(staging.resolve())], capture_output=True, text=True)
    if signed.returncode or not _sound(staging, run):
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"codesign refused it: {signed.stderr.strip()}")
    retired = bundle.with_name(bundle.name + ".old")
    shutil.rmtree(retired, ignore_errors=True)
    if bundle.exists():
        bundle.rename(retired)
    try:
        staging.rename(bundle)
    except OSError:
        if retired.exists() and not bundle.exists():
            retired.rename(bundle)
        raise
    shutil.rmtree(retired, ignore_errors=True)


def tools(run: Run) -> bool:
    """Whether the Command Line Tools are installed. Without them `cc` and `xcrun` are shims that
    raise the install dialog on the owner's screen, so nothing calls them first."""
    try:
        return run(["xcode-select", "-p"], capture_output=True, text=True).returncode == 0
    except OSError:
        return False


def ensure(root: Path, release: Path, run: Run = subprocess.run) -> Path | None:
    """The launcher's executable under `root`, built from `release` only if missing, unsound or built
    from other source; None off macOS, or when none can be had, and the job then runs python itself."""
    if sys.platform != "darwin":
        return None
    bundle = root / BUNDLE
    source = release / SOURCE
    before = _sound(bundle, run)
    try:
        # A release older than the launcher has no source: the one already built stays.
        want = digest(source)
        if before and _built_from(bundle) == want:
            return executable(bundle)
        if not tools(run):
            raise RuntimeError("no Command Line Tools (xcode-select --install)")
        _build(bundle, source, want, run)
    except (OSError, RuntimeError) as error:
        if before and _sound(bundle, run):
            print(f"launcher: not rebuilt ({error}); the job keeps the one it has", file=sys.stderr)
            return executable(bundle)
        print(f"launcher: not built ({error}); privacy prompts stay attributed to python", file=sys.stderr)
        return None
    # Every build is a new signature, whatever caused it, so every one says what it costs.
    print(f'launcher: built {bundle}; grant "2mw2lt agent" (doc 141 §3), and retire the sessions '
          'launched before it, whose tmux servers keep the signature they started under', file=sys.stderr)
    return executable(bundle)
