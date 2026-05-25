from __future__ import annotations

import hashlib
import importlib
import importlib.util
import shutil
import sys
import urllib.request
from pathlib import Path

import maya.cmds as cmds


DEFAULT_DEMBONES_CLI_URLS = [
    "https://raw.githubusercontent.com/electronicarts/dem-bones/master/bin/Windows/DemBones.exe",
    "https://github.com/electronicarts/dem-bones/raw/master/bin/Windows/DemBones.exe",
    "https://github.com/electronicarts/dem-bones/releases/latest/download/DemBones.exe",
]
DEFAULT_DEMBONES_CLI_SHA256 = ""

PACKAGE_SRC_NAME = "db_export_v3"
PACKAGE_INSTALL_NAME = "db_export_v3"
MODULE_DISPLAY_NAME = "DB_export_v3"


def _safe_mod_version(version_text: str) -> str:
    value = str(version_text or "").strip()
    if not value or value in {"missing", "unknown"}:
        return "1.0"
    return value


def _write_mod_file(mod_file: Path, module_root: Path, module_version: str) -> None:
    module_root_str = str(module_root).replace("\\", "/")
    text = (
        "+ {0} {1} {2}\n"
        "PYTHONPATH +:= scripts\n"
        "PATH +:= bin\n"
    ).format(MODULE_DISPLAY_NAME, _safe_mod_version(module_version), module_root_str)
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
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
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


def _maya_modules_dir() -> Path:
    maya_ver = str(cmds.about(version=True))
    maya_app = Path(cmds.internalVar(userAppDir=True))
    return maya_app / maya_ver / "modules"


def _candidate_cli_paths(repo_root: Path) -> list[Path]:
    modules_dir = _maya_modules_dir()
    candidates = [
        repo_root / "bin" / "DemBones.exe",
        modules_dir / MODULE_DISPLAY_NAME / "bin" / "DemBones.exe",
        modules_dir / "DB_export_v2" / "bin" / "DemBones.exe",
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        norm = str(path.resolve()) if path.exists() else str(path)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(path)
    return out


def _try_copy_existing_cli(bin_dst: Path, repo_root: Path) -> Path | None:
    cli_dst = bin_dst / "DemBones.exe"
    if cli_dst.exists():
        print("{0} CLI: using cached binary: {1}".format(MODULE_DISPLAY_NAME, str(cli_dst)))
        return cli_dst

    for candidate in _candidate_cli_paths(repo_root):
        if not candidate.exists() or not candidate.is_file():
            continue
        bin_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(candidate), str(cli_dst))
        print("{0} CLI: copied existing binary from: {1}".format(MODULE_DISPLAY_NAME, str(candidate)))
        return cli_dst
    return None


def _ensure_dem_bones_cli(bin_dst: Path, repo_root: Path) -> Path:
    cli_dst = _try_copy_existing_cli(bin_dst, repo_root)
    if cli_dst is not None:
        return cli_dst

    cli_dst = bin_dst / "DemBones.exe"
    expected_sha = DEFAULT_DEMBONES_CLI_SHA256.strip().lower()
    last_error = None
    for url in DEFAULT_DEMBONES_CLI_URLS:
        try:
            print("{0} CLI: downloading from: {1}".format(MODULE_DISPLAY_NAME, url))
            _download_file(url, cli_dst)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            print("{0} CLI: download failed: {1}".format(MODULE_DISPLAY_NAME, exc))
    if last_error is not None:
        raise RuntimeError("All CLI acquisition paths failed. Last error: {0}".format(last_error))

    actual_sha = _sha256_file(cli_dst)
    print("{0} CLI: downloaded sha256: {1}".format(MODULE_DISPLAY_NAME, actual_sha))
    if expected_sha and actual_sha != expected_sha:
        try:
            cli_dst.unlink()
        except Exception:
            pass
        raise RuntimeError(
            "CLI checksum mismatch. expected={0} actual={1}".format(expected_sha, actual_sha)
        )
    return cli_dst


def _load_installed_package_from_path(pkg_name: str, pkg_dir: Path):
    init_py = pkg_dir / "__init__.py"
    if not init_py.exists():
        raise RuntimeError("Installed package init not found: {0}".format(init_py))

    stale = [name for name in list(sys.modules.keys()) if name == pkg_name or name.startswith(pkg_name + ".")]
    for name in stale:
        try:
            del sys.modules[name]
        except Exception:
            pass

    importlib.invalidate_caches()

    spec = importlib.util.spec_from_file_location(
        pkg_name,
        str(init_py),
        submodule_search_locations=[str(pkg_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to create import spec for: {0}".format(init_py))

    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return module


def install_db_export_v3(open_ui: bool = True) -> str:
    repo_root = Path(__file__).resolve().parent
    pkg_src = repo_root / PACKAGE_SRC_NAME
    if not pkg_src.exists():
        raise RuntimeError("Source package not found: {0}".format(pkg_src))

    maya_ver = str(cmds.about(version=True))
    maya_app = Path(cmds.internalVar(userAppDir=True))
    modules_dir = maya_app / maya_ver / "modules"
    module_root = modules_dir / MODULE_DISPLAY_NAME
    scripts_dst = module_root / "scripts" / PACKAGE_INSTALL_NAME
    bin_dst = module_root / "bin"
    mod_file = modules_dir / (MODULE_DISPLAY_NAME + ".mod")

    source_version = _read_version_from_file(pkg_src / "version.py")
    print("{0} source root: {1}".format(MODULE_DISPLAY_NAME, str(repo_root)))
    print("{0} source version: {1}".format(MODULE_DISPLAY_NAME, source_version))

    _copy_tree(pkg_src, scripts_dst)
    bin_dst.mkdir(parents=True, exist_ok=True)
    try:
        cli_path = _ensure_dem_bones_cli(bin_dst, repo_root)
        print("{0} CLI installed: {1}".format(MODULE_DISPLAY_NAME, str(cli_path)))
    except Exception as exc:
        raise RuntimeError(
            "Failed to acquire DemBones CLI. Installer first tries existing local binaries and only then download fallback. "
            "Details: {0}".format(exc)
        )
    _write_mod_file(mod_file, module_root, source_version)

    installed_version_file = scripts_dst / "version.py"
    installed_version = _read_version_from_file(installed_version_file)
    print("{0} installed path: {1}".format(MODULE_DISPLAY_NAME, str(scripts_dst)))
    print("{0} installed version file: {1}".format(MODULE_DISPLAY_NAME, installed_version))

    scripts_parent = str(module_root / "scripts")
    if scripts_parent not in sys.path:
        sys.path.insert(0, scripts_parent)
    else:
        try:
            sys.path.remove(scripts_parent)
        except Exception:
            pass
        sys.path.insert(0, scripts_parent)

    if open_ui:
        pkg = _load_installed_package_from_path(PACKAGE_INSTALL_NAME, scripts_dst)
        version = getattr(pkg, "VERSION", "unknown")
        print("{0} loaded version: {1}".format(MODULE_DISPLAY_NAME, version))
        print("{0} loaded file: {1}".format(MODULE_DISPLAY_NAME, getattr(pkg, "__file__", "unknown")))
        main_fn = getattr(pkg, "main", None)
        if callable(main_fn):
            main_fn()
        else:
            pkg.open_window()

    return str(module_root)


def onMayaDroppedPythonFile(*_args):
    try:
        dst = install_db_export_v3(open_ui=True)
        try:
            import db_export_v3  # type: ignore

            version_text = getattr(db_export_v3, "VERSION", "unknown")
        except Exception:
            version_text = "unknown"
        cmds.inViewMessage(
            amg='<hl>{0} installed</hl> v{1}<br>{2}'.format(
                MODULE_DISPLAY_NAME, version_text, dst.replace("\\", "/")
            ),
            pos="midCenter",
            fade=True,
        )
    except Exception as exc:
        cmds.confirmDialog(title="{0} install failed".format(MODULE_DISPLAY_NAME), message=str(exc), button=["OK"])
