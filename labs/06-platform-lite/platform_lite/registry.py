"""平台 v0.1 的"资产管理"层：读取登记表，解析成可调度的套件对象。

刻意只做三件事：读、校验、解析路径。平台的复杂度应该长在调度与治理上，
而不是长在配置解析上 —— 这是自建平台最常见的跑偏方向。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "registry" / "suites.yaml"


class RegistryError(ValueError):
    """登记表本身有问题时抛出：平台要在启动阶段就失败，而不是跑到一半。"""


@dataclass(frozen=True)
class Suite:
    id: str
    name: str
    cwd: Path
    command: tuple[str, ...]
    tags: tuple[str, ...]
    kind: str
    shardable: bool
    timeout_s: int
    owner: str

    def supports(self, tag: str | None) -> bool:
        return tag is None or tag in self.tags


@dataclass(frozen=True)
class Gate:
    max_failures: int
    max_duration_s: float
    require_pass_rate: float


def load(path: Path | None = None) -> tuple[list[Suite], Gate, dict[str, dict]]:
    registry_path = path or REGISTRY_PATH
    if not registry_path.exists():
        raise RegistryError(f"登记表不存在: {registry_path}")

    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    base = registry_path.parent

    gate_raw = raw.get("gate") or {}
    gate = Gate(
        max_failures=int(gate_raw.get("max_failures", 0)),
        max_duration_s=float(gate_raw.get("max_duration_s", 600)),
        require_pass_rate=float(gate_raw.get("require_pass_rate", 1.0)),
    )

    suites: list[Suite] = []
    seen: set[str] = set()
    for item in raw.get("suites") or []:
        suite_id = item.get("id")
        if not suite_id:
            raise RegistryError("套件缺少 id")
        if suite_id in seen:
            raise RegistryError(f"套件 id 重复: {suite_id}")
        seen.add(suite_id)

        cwd = (base / item.get("cwd", ".")).resolve()
        if not cwd.exists():
            raise RegistryError(f"套件 {suite_id} 的工作目录不存在: {cwd}")

        command = tuple(str(part).replace("{python}", sys.executable) for part in item.get("command", []))
        if not command:
            raise RegistryError(f"套件 {suite_id} 缺少 command")

        suites.append(
            Suite(
                id=suite_id,
                name=item.get("name", suite_id),
                cwd=cwd,
                command=command,
                tags=tuple(item.get("tags", [])),
                kind=item.get("kind", "test"),
                shardable=bool(item.get("shardable", False)),
                timeout_s=int(item.get("timeout_s", 300)),
                owner=item.get("owner", "unknown"),
            )
        )

    if not suites:
        raise RegistryError("登记表中没有任何套件")
    return suites, gate, raw.get("environments", {})