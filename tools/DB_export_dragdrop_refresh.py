from __future__ import annotations

# One-time refresh launcher for Maya drag-and-drop.
# Use this file if Maya cached an old DB_export_dragdrop module in memory.

import importlib
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _purge_cached_modules():
    prefixes = ("db_export", "db_export_local_installer", "DB_export_dragdrop")
    for name in list(sys.modules.keys()):
        low = name.lower()
        if (
            low == "db_export"
            or low.startswith("db_export.")
            or low.startswith("db_export_local_installer")
            or low.startswith("db_export_dragdrop")
        ):
            try:
                del sys.modules[name]
            except Exception:
                pass
    importlib.invalidate_caches()


def _load_local_installer():
    installer_path = _ROOT / "tools" / "db_export_install.py"
    if not installer_path.exists():
        raise RuntimeError("Installer not found: {0}".format(installer_path))

    spec = importlib.util.spec_from_file_location("db_export_local_installer", str(installer_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load installer spec: {0}".format(installer_path))

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def onMayaDroppedPythonFile(*args):
    _purge_cached_modules()
    installer = _load_local_installer()
    return installer.onMayaDroppedPythonFile(*args)


def install_db_export(open_ui=True):
    _purge_cached_modules()
    installer = _load_local_installer()
    return installer.install_db_export(open_ui=open_ui)


if __name__ == "__main__":
    install_db_export(open_ui=True)

