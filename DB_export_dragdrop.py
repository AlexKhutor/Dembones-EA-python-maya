from __future__ import annotations

# Maya drag-and-drop entry point.
# Drop this file into Maya viewport/Script Editor to install DB_export.

import sys
import importlib.util
import importlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Defensive cleanup for stale in-memory modules from previous installs.
for _name in list(sys.modules.keys()):
    if _name == "db_export" or _name.startswith("db_export.") or _name.startswith("db_export_local_installer"):
        try:
            del sys.modules[_name]
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
    try:
        dragdrop_file = Path(__file__).resolve()
        stamp = dragdrop_file.stat().st_mtime
        print("DB_export dragdrop file:", str(dragdrop_file))
        print("DB_export dragdrop file mtime:", stamp)
    except Exception:
        pass
    installer = _load_local_installer()
    return installer.onMayaDroppedPythonFile(*args)


def install_db_export(open_ui=True):
    installer = _load_local_installer()
    return installer.install_db_export(open_ui=open_ui)


if __name__ == "__main__":
    install_db_export(open_ui=True)
