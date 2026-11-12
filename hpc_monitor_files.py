"""Directory listings shared by the X11 and Python web file pickers."""
from pathlib import Path

from hpc_monitor_core import natural_key


def directory_listing(path, base=None):
    if not isinstance(path, str) or (base is not None and not isinstance(base, str)):
        raise ValueError("폴더 경로를 입력하세요.")
    root = Path(path or ".").expanduser()
    if not root.is_absolute():
        root = Path(base or Path.cwd()) / root
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("폴더 경로를 입력하세요: " + str(root))
    entries = []
    for child in root.iterdir():
        try:
            directory = child.is_dir()
            if not directory and not child.is_file():
                continue
        except OSError:
            continue
        kind = "folder" if directory else "session" if child.suffix.lower() == ".json" else "log" if child.suffix.lower() in (".tsv", ".log", ".txt", ".csv") else "other"
        entries.append(dict(name=child.name, path=str(child), directory=directory, kind=kind))
    entries.sort(key=lambda row: (not row["directory"], natural_key(row["name"])))
    return dict(path=str(root), parent=str(root.parent), entries=entries)
