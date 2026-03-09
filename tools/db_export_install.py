from __future__ import annotations

import hashlib
import shutil
import sys
import importlib
import importlib.util
import urllib.request
from pathlib import Path

import maya.cmds as cmds


DEFAULT_DEMBONES_CLI_URLS = [
    # Preferred: direct file from original repository.
    "https://raw.githubusercontent.com/electronicarts/dem-bones/master/bin/Windows/DemBones.exe",
    # Fallback: GitHub raw endpoint.
    "https://github.com/electronicarts/dem-bones/raw/master/bin/Windows/DemBones.exe",
    # Legacy release alias (may be unavailable).
    "https://github.com/electronicarts/dem-bones/releases/latest/download/DemBones.exe",
]
# Optional strict pin. Leave empty to skip hash validation.
DEFAULT_DEMBONES_CLI_SHA256 = ""


def _write_mod_file(mod_file: Path, module_root: Path) -> None:
    module_root_str = str(module_root).replace("\\", "/")
    text = (
        "+ DB_export 1.0 {0}\n"
        "PYTHONPATH +:= scripts\n"
        "PATH +:= bin\n"
    ).format(module_root_str)
    mod_file.parent.mkdir(parents=True, exist_ok=True)
    mod_file.write_text(text, encoding="ascii")


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _read_version_from_file(version_py: Path) -> str:
    if not version_py.exists():
        return "missing"
    text = version_py.read_text(encoding="utf-8", errors="replace")
    marker = 'VERSION = "'
    idx = text.find(marker)
    if idx < 0:
        return "unknown"
    start = idx + len(marker)
    end = text.find('"', start)
    if end < 0:
        return "unknown"
    return text[start:end]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def _download_file(url: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".download")
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    if dst.exists():
        dst.unlink()
    tmp.replace(dst)


def _ensure_dem_bones_cli(bin_dst: Path) -> Path:
    cli_dst = bin_dst / "DemBones.exe"
    if cli_dst.exists():
        print("DB_export CLI: using cached binary:", str(cli_dst))
        return cli_dst

    expected_sha = DEFAULT_DEMBONES_CLI_SHA256.strip().lower()
    last_error = None
    for url in DEFAULT_DEMBONES_CLI_URLS:
        try:
            print("DB_export CLI: downloading from:", url)
            _download_file(url, cli_dst)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            print("DB_export CLI: download failed:", exc)
    if last_error is not None:
        raise RuntimeError(
            "All download URLs failed. Last error: {0}".format(last_error)
        )

    actual_sha = _sha256_file(cli_dst)
    print("DB_export CLI: downloaded sha256:", actual_sha)
    if expected_sha and actual_sha != expected_sha:
        try:
            cli_dst.unlink()
        except Exception:
            pass
        raise RuntimeError(
            "CLI checksum mismatch. expected={0} actual={1}".format(expected_sha, actual_sha)
        )
    return cli_dst


def _load_installed_package_from_path(pkg_dir: Path):
    init_py = pkg_dir / "__init__.py"
    if not init_py.exists():
        raise RuntimeError("Installed package init not found: {0}".format(init_py))

    # Remove stale package/modules from current Maya interpreter.
    stale = [name for name in list(sys.modules.keys()) if name == "db_export" or name.startswith("db_export.")]
    for name in stale:
        try:
            del sys.modules[name]
        except Exception:
            pass

    importlib.invalidate_caches()

    spec = importlib.util.spec_from_file_location(
        "db_export",
        str(init_py),
        submodule_search_locations=[str(pkg_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to create import spec for: {0}".format(init_py))

    module = importlib.util.module_from_spec(spec)
    sys.modules["db_export"] = module
    spec.loader.exec_module(module)
    return module


def install_db_export(open_ui: bool = True) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    pkg_src = repo_root / "db_export"

    if not pkg_src.exists():
        raise RuntimeError("Source package not found: {0}".format(pkg_src))

    maya_ver = str(cmds.about(version=True))
    maya_app = Path(cmds.internalVar(userAppDir=True))
    modules_dir = maya_app / maya_ver / "modules"
    module_root = modules_dir / "DB_export"
    scripts_dst = module_root / "scripts" / "db_export"
    bin_dst = module_root / "bin"
    mod_file = modules_dir / "DB_export.mod"

    source_version = _read_version_from_file(pkg_src / "version.py")
    print("DB_export source root:", str(repo_root))
    print("DB_export source version:", source_version)

    _copy_tree(pkg_src, scripts_dst)
    bin_dst.mkdir(parents=True, exist_ok=True)
    try:
        cli_path = _ensure_dem_bones_cli(bin_dst)
        print("DB_export CLI installed:", str(cli_path))
    except Exception as exc:
        raise RuntimeError(
            "Failed to acquire DemBones CLI from original source. "
            "Check internet access or set CLI exe path manually in UI after install. "
            "Details: {0}".format(exc)
        )
    _write_mod_file(mod_file, module_root)

    installed_version_file = scripts_dst / "version.py"
    installed_version = _read_version_from_file(installed_version_file)
    print("DB_export installed path:", str(scripts_dst))
    print("DB_export installed version file:", installed_version)

    scripts_parent = str(module_root / "scripts")
    if scripts_parent not in sys.path:
        sys.path.insert(0, scripts_parent)
    # keep scripts_parent at front so "import db_export" resolves to module install
    else:
        try:
            sys.path.remove(scripts_parent)
        except Exception:
            pass
        sys.path.insert(0, scripts_parent)

    if open_ui:
        db_export = _load_installed_package_from_path(scripts_dst)
        version = getattr(db_export, "VERSION", "unknown")
        print("DB_export loaded version:", version)
        print("DB_export loaded file:", getattr(db_export, "__file__", "unknown"))
        db_export.open_window()

    return str(module_root)


def onMayaDroppedPythonFile(*_args):
    try:
        dst = install_db_export(open_ui=True)
        try:
            import db_export  # type: ignore

            version_text = getattr(db_export, "VERSION", "unknown")
        except Exception:
            version_text = "unknown"
        cmds.inViewMessage(
            amg='<hl>DB_export installed</hl> v{0}<br>{1}'.format(
                version_text, dst.replace("\\", "/")
            ),
            pos="midCenter",
            fade=True,
        )
    except Exception as exc:
        cmds.confirmDialog(title="DB_export install failed", message=str(exc), button=["OK"])
