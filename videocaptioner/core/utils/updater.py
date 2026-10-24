"""便携版增量更新。

设计目标：
- 只下载改动的文件，不重下 328 MB
- 不动 app\\AppData（设置、缓存、模型）和 app\\work-dir（工作文件）
- 更新失败能回滚，不会留下半新半旧的程序

工作方式：
1. 更新包是一个 zip，内含 update_manifest.json + 变更的文件
2. 清单记录每个文件的相对路径、大小、SHA-256
3. 先全部解压到临时目录并校验，通过后再逐个替换
4. 替换前把原文件备份，全部成功后删除备份
"""

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("updater")

MANIFEST_NAME = "update_manifest.json"
BACKUP_SUFFIX = ".vcupdate-backup"

# 这些目录属于用户数据，更新时绝对不碰
PROTECTED_PREFIXES = (
    "app/AppData/",
    "app/work-dir/",
    "app/resource/bin/Faster-Whisper-XXL/",
)


@dataclass
class UpdateFile:
    path: str       # 相对便携根目录，正斜杠
    size: int
    sha256: str


@dataclass
class UpdateManifest:
    version: str
    files: List[UpdateFile]
    notes: str = ""
    min_from_version: str = ""

    @staticmethod
    def from_dict(data: dict) -> "UpdateManifest":
        files = [
            UpdateFile(
                path=f["path"],
                size=int(f.get("size", 0)),
                sha256=(f.get("sha256") or "").lower(),
            )
            for f in data.get("files", [])
        ]
        return UpdateManifest(
            version=str(data.get("version", "")),
            files=files,
            notes=str(data.get("notes", "")),
            min_from_version=str(data.get("min_from_version", "")),
        )


def portable_root() -> Path:
    """便携版根目录（含 app/ 与 runtime/ 的那一层）。"""
    here = Path(__file__).resolve()
    # <root>/app/videocaptioner/core/utils/updater.py
    return here.parents[4]


def app_version() -> str:
    try:
        from videocaptioner._version import __version__

        return __version__.split(".dev")[0]
    except Exception:
        return "0.0.0"


def is_protected(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").lstrip("/")
    return any(normalized.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def read_manifest_from_zip(zip_path: str) -> UpdateManifest:
    """读取更新包的清单，不解压文件。"""
    with zipfile.ZipFile(zip_path) as zf:
        if MANIFEST_NAME not in zf.namelist():
            raise ValueError(
                f"更新包里没有 {MANIFEST_NAME}，可能不是有效的增量更新包"
            )
        data = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    return UpdateManifest.from_dict(data)


def verify_update_zip(
    zip_path: str,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str, Optional[UpdateManifest]]:
    """校验更新包完整性与哈希。"""
    try:
        manifest = read_manifest_from_zip(zip_path)
    except Exception as e:
        return False, f"读取清单失败：{e}", None

    if not manifest.files:
        return False, "更新包里没有任何文件", manifest

    bad = [f.path for f in manifest.files if is_protected(f.path)]
    if bad:
        logger.warning("更新包试图修改受保护路径，已拒绝: %s", bad[:3])
        return False, "更新包包含不允许修改的路径（用户数据区）", manifest

    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            for i, item in enumerate(manifest.files, 1):
                if item.path not in names:
                    return False, f"更新包缺少文件：{item.path}", manifest
                if progress:
                    progress(f"正在校验 {i}/{len(manifest.files)}：{item.path}")
                with zf.open(item.path) as fh:
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                if item.size and size != item.size:
                    return False, f"文件大小不符：{item.path}", manifest
                if item.sha256 and digest.hexdigest().lower() != item.sha256:
                    return False, f"文件校验失败：{item.path}", manifest
    except zipfile.BadZipFile as e:
        return False, f"更新包已损坏：{e}", manifest

    return True, "", manifest


def apply_update(
    zip_path: str,
    root: Optional[Path] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """应用增量更新包。

    流程：解压到临时目录 -> 再次校验 -> 备份原文件 -> 替换 -> 清理。
    任一环节失败都会把已替换的文件还原。

    Args:
        zip_path: 更新包路径
        root: 便携版根目录，默认自动推断
        progress: 进度回调

    Returns:
        (是否成功, 说明)
    """
    root = Path(root or portable_root())
    if not root.is_dir():
        return False, f"找不到程序目录：{root}"

    ok, reason, manifest = verify_update_zip(zip_path, progress)
    if not ok or manifest is None:
        return False, reason

    if progress:
        progress("正在解压更新文件…")

    staging = Path(tempfile.mkdtemp(prefix="vcupdate-", dir=str(root.parent)))
    backups: List[tuple[Path, Optional[Path]]] = []

    try:
        with zipfile.ZipFile(zip_path) as zf:
            for item in manifest.files:
                target = staging / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(item.path) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        # 再核对一次解压结果，防止解压过程中出错
        for item in manifest.files:
            target = staging / item.path
            if not target.is_file():
                raise RuntimeError(f"解压后缺少文件：{item.path}")
            if item.sha256 and sha256_file(target) != item.sha256:
                raise RuntimeError(f"解压后校验失败：{item.path}")

        # 备份并替换
        for i, item in enumerate(manifest.files, 1):
            if progress:
                progress(f"正在更新 {i}/{len(manifest.files)}：{item.path}")

            dest = root / item.path
            dest.parent.mkdir(parents=True, exist_ok=True)

            backup = None
            if dest.exists():
                backup = dest.with_name(dest.name + BACKUP_SUFFIX)
                if backup.exists():
                    backup.unlink()
                shutil.copy2(dest, backup)
            backups.append((dest, backup))

            shutil.copy2(staging / item.path, dest)

        # 全部成功：删除备份
        for _, backup in backups:
            if backup and backup.exists():
                try:
                    backup.unlink()
                except OSError:
                    pass

        logger.info("增量更新完成，版本 %s，共 %d 个文件", manifest.version, len(manifest.files))
        return True, f"已更新到 {manifest.version}，共 {len(manifest.files)} 个文件"

    except Exception as e:
        logger.exception("增量更新失败，开始回滚")
        if progress:
            progress("更新失败，正在还原…")

        for dest, backup in reversed(backups):
            try:
                if backup and backup.exists():
                    shutil.copy2(backup, dest)
                    backup.unlink()
                elif not backup and dest.exists():
                    dest.unlink()
            except Exception as rollback_error:
                logger.error("回滚 %s 失败: %s", dest, rollback_error)

        return False, f"更新失败（已还原）：{e}"

    finally:
        shutil.rmtree(staging, ignore_errors=True)


def pending_restart_needed(zip_manifest: UpdateManifest) -> bool:
    """更新涉及可执行文件时需要重启程序才生效。"""
    for item in zip_manifest.files:
        if item.path.lower().endswith((".exe", ".dll", ".pyd")):
            return True
    return False
