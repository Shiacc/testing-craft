"""缺陷去重存储：``atf/state/filed_bugs.json``，键是指纹，值是提单结果。

为什么用 JSON 文件而不是数据库：
去重表要在 CI 的每次运行之间**跨进程、跨机器（工作区挂载）**存活，但读写频率极低
（一次运行最多几十条），数据量极小（一条几十字节）。SQLite 会引入"文件被占用 / 需要
WAL"的运维话题；JSON 能被人类直接打开确认"这条真提过了"，出问题时用编辑器就能修。

为什么每次读写都重新从磁盘加载、并做原子替换：
CI 里多个分片进程可能同时跑失败用例、同时提单。进程内锁挡不住进程间竞争，所以
「读盘 → 改 → 写临时文件 → os.replace 原子替换」是这里唯一可靠的最小方案。
检测与写入之间的窗口仍有竞态，但代价只是"偶发重复提单一次"，比"丢记录导致后续全部
重复提单"轻得多——这个取舍是刻意选的。

文件损坏（半截写入、手工改坏）时默认不阻断流程：把坏文件备份成 ``*.corrupt-*`` 再当作
空表继续，缺陷该提还是提；``strict=True`` 则在此时抛错，供"宁可红也不丢状态"的场景使用。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from .models import BugFilerError

# 默认落在 atf/state/filed_bugs.json：与 atf 下其它模块共享的运行时状态目录。
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / "state" / "filed_bugs.json"
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# 进程内按"绝对路径"共享锁：同一个进程里多个 DedupeStore 实例指向同一文件时也串行。
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class DedupeStore:
    """指纹 → 已提缺陷记录。记录形如 ``{"tracker": "zentao", "key": "101", "url": ..., "filed_at": ...}``。"""

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH, *, strict: bool = False) -> None:
        self._path = Path(path)
        self._strict = strict
        self._lock = _lock_for(self._path)

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------ 查询

    def check(self, fingerprint: str) -> dict | None:
        """已提过则返回那条记录，否则 None。

        返回的是**副本**：调用方随手改它不该污染磁盘上的真相。
        """
        key = (fingerprint or "").strip()
        if not key:
            raise BugFilerError("DedupeStore.check 需要非空指纹（BugDraft.fingerprint）")
        with self._lock:
            data = self._load()
        record = data.get(key)
        if not isinstance(record, dict):
            return None
        return dict(record)

    def entries(self) -> dict[str, dict]:
        """整张表的副本，供 CLI / 报告做审计。"""
        with self._lock:
            data = self._load()
        return {key: dict(value) for key, value in data.items()}

    def __len__(self) -> int:
        return len(self.entries())

    def __contains__(self, fingerprint: object) -> bool:
        return isinstance(fingerprint, str) and self.check(fingerprint) is not None

    # ------------------------------------------------------------ 写入

    def record(self, fingerprint: str, tracker: str, key: str, url: str) -> dict:
        """登记一条已提缺陷，返回写入的完整记录（含 filed_at）。

        同一指纹重复 record 会覆盖：以最后一次成功的提单为准——上一次可能是被人工关掉的
        重复单，用户重新提一次就该指向新单。
        """
        fp = (fingerprint or "").strip()
        if not fp:
            raise BugFilerError("DedupeStore.record 需要非空指纹（BugDraft.fingerprint）")
        tracker_name = (tracker or "").strip()
        if not tracker_name:
            raise BugFilerError("DedupeStore.record 需要 tracker（zentao / jira）")

        entry = {
            "tracker": tracker_name,
            "key": str(key or "").strip(),
            "url": str(url or "").strip(),
            "filed_at": datetime.now().strftime(STAMP_FORMAT),
        }
        with self._lock:
            data = self._load()
            data[fp] = entry
            self._save(data)
        return dict(entry)

    def forget(self, fingerprint: str) -> bool:
        """删掉一条记录（人工确认"这个缺陷要重提"时用）。返回是否真的删掉了。"""
        fp = (fingerprint or "").strip()
        if not fp:
            raise BugFilerError("DedupeStore.forget 需要非空指纹")
        with self._lock:
            data = self._load()
            if fp not in data:
                return False
            del data[fp]
            self._save(data)
        return True

    # ------------------------------------------------------------ 内部

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            backup = self._quarantine()
            if self._strict:
                raise BugFilerError(f"去重状态文件无法解析：{self._path}（已备份到 {backup}）") from exc
            return {}
        if not isinstance(raw, dict):
            backup = self._quarantine()
            if self._strict:
                raise BugFilerError(f"去重状态文件顶层不是对象：{self._path}（已备份到 {backup}）")
            return {}
        # 只信任结构正确的条目：手工编辑可能留下字符串值，别让它在调用方炸成 AttributeError。
        return {str(fp): value for fp, value in raw.items() if isinstance(value, dict)}

    def _save(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            tmp.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(tmp, self._path)  # 原子替换：读者永远看不到写了一半的表
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def _quarantine(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            os.replace(self._path, backup)
        except OSError:
            return self._path
        return backup

    def __repr__(self) -> str:  # 调试输出不带内容，避免把指纹与 URL 灌进日志
        return f"DedupeStore(path={str(self._path)!r}, entries={len(self)})"


def describe(record: dict) -> str:
    """把一条记录渲染成 CLI 可读的一行（不含任何凭据）。"""
    tracker = record.get("tracker", "?")
    key = record.get("key", "?")
    url = record.get("url", "")
    filed_at = record.get("filed_at", "?")
    return f"{tracker}:{key} {filed_at} {url}".strip()
