"""Private local files: no permissive creation window, no symlink destinations."""
import json
import os
from pathlib import Path
import tempfile
import sys


def private_dir(path):
    path = Path(path).absolute()
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            if sys.platform == "darwin" and str(component) in ("/var", "/tmp") and component.resolve() == Path("/private") / component.name:
                continue
            raise ValueError("Private path must not contain symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def atomic_json(path, data):
    path = Path(path)
    private_dir(path.parent)
    if path.is_symlink():
        raise ValueError("Refusing symlink destination")
    fd, temp = tempfile.mkstemp(prefix=".private-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def private_log(path, reset=False):
    path = Path(path)
    private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        if reset:
            os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def report_path(path, *, forbidden=(), extensions=(".md", ".txt", ".json")):
    path = Path(path).expanduser().absolute()
    # Inspect links before resolve; atomic replacement must not hide an escape.
    for item in [path, *path.parents]:
        if item.is_symlink():
            if sys.platform == "darwin" and str(item) in ("/var", "/tmp") and item.resolve() == Path("/private") / item.name:
                continue
            raise ValueError("Report path must not contain symlinks")
    if path.suffix.lower() not in extensions:
        raise ValueError("Report output must have a supported document extension")
    resolved = path.resolve()
    protected = [Path(p).expanduser().resolve() for p in forbidden if p is not None]
    protected += [Path.home() / ".config", Path.home() / "Library/Containers/com.tencent.xinWeChat"]
    for item in protected:
        if resolved == item or item in resolved.parents:
            raise ValueError("Report output overlaps a protected data/configuration directory")
    if path.exists() and path.stat().st_nlink != 1:
        raise ValueError("Report output has hard links")
    return path


def atomic_text(path, content, *, forbidden=(), extensions=(".md", ".txt", ".json")):
    path = report_path(path, forbidden=forbidden, extensions=extensions)
    private_dir(path.parent)
    fd, temp = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
