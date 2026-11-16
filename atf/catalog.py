"""用例库（Catalog）：用例资产的唯一真相源。

四个能力：
1. **加载与校验**：字段、枚举、ID 唯一、自动化绑定是否真的存在；
2. **覆盖率**：按层级 / 优先级统计自动化率（口径固定，防止"改字段造数"）；
3. **孤儿检测**：有测试但无用例（黑户）、有用例但绑定失效（悬空）；
4. **回写结果**：执行后把 last_run / last_result 写回，形成"用例活着"的证据。

设计要点：**覆盖率只认可解析的绑定**。`automated_by` 解析不到真实测试时，
该用例自动降级为 manual 并在报告里标注，绝不虚增自动化率。
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "atf" / "cases" / "cases.yaml"
LAB_DIR = ROOT / "labs" / "07-real-project"

LAYERS = {"L1", "L2", "L3", "L4", "L5"}
PRIORITIES = {"P0", "P1", "P2"}
STATUSES = {"automated", "manual", "deprecated"}
REQUIRED = ("id", "title", "layer", "priority", "status", "owner")


@dataclass
class Case:
    id: str
    title: str
    layer: str
    priority: str
    status: str
    owner: str
    tags: list[str] = field(default_factory=list)
    requirement: str = ""
    automated_by: str = ""
    last_run: str = ""
    last_result: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "layer": self.layer,
            "priority": self.priority,
            "tags": self.tags,
            "requirement": self.requirement,
            "status": self.status,
            "automated_by": self.automated_by,
            "owner": self.owner,
            "last_run": self.last_run,
            "last_result": self.last_result,
            "notes": self.notes,
        }


@dataclass
class Catalog:
    data: dict
    cases: list[Case]
    path: Path = CASES_PATH

    @classmethod
    def load(cls, path: Path | None = None) -> "Catalog":
        target = path or CASES_PATH
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        cases = [Case(**{k: v for k, v in item.items() if k in Case.__dataclass_fields__}) for item in raw.get("cases", [])]
        return cls(data=raw, cases=cases, path=target)

    def save(self) -> None:
        self.data["cases"] = [case.to_dict() for case in self.cases]
        self.path.write_text(
            yaml.safe_dump(self.data, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
            newline="\n",
        )

    def by_id(self, case_id: str) -> Case | None:
        return next((case for case in self.cases if case.id == case_id), None)

    def by_test(self, nodeid: str) -> Case | None:
        for case in self.cases:
            if case.automated_by and (_normalize(case.automated_by) == _normalize(nodeid)):
                return case
        return None

    # ------------------------------------------------------------ 校验

    def validate(self) -> tuple[list[str], list[str]]:
        """返回 (errors, warnings)。errors 必须为 0 才算合格。"""
        errors: list[str] = []
        warnings: list[str] = []
        seen: set[str] = set()

        for case in self.cases:
            for field_name in REQUIRED:
                if not getattr(case, field_name):
                    errors.append(f"{case.id or '(无ID)'}: 缺少必填字段 {field_name}")
            if case.id in seen:
                errors.append(f"{case.id}: ID 重复")
            seen.add(case.id)
            if case.layer not in LAYERS:
                errors.append(f"{case.id}: layer 非法 {case.layer}")
            if case.priority not in PRIORITIES:
                errors.append(f"{case.id}: priority 非法 {case.priority}")
            if case.status not in STATUSES:
                errors.append(f"{case.id}: status 非法 {case.status}")
            if case.status == "automated" and not case.automated_by:
                errors.append(f"{case.id}: status=automated 但未绑定 automated_by")
            if case.status != "automated" and case.automated_by:
                errors.append(f"{case.id}: status={case.status} 不应有 automated_by")
            if case.automated_by and not case.automated_by.startswith("tests/"):
                warnings.append(f"{case.id}: automated_by 建议使用 labs 内相对路径（tests/...）")

        collected = collect_tests()
        if collected:
            for case in self.cases:
                if case.status == "automated" and case.automated_by:
                    if _normalize(case.automated_by) not in collected:
                        errors.append(f"{case.id}: 绑定的测试不存在 → {case.automated_by}")
            bound = {_normalize(case.automated_by) for case in self.cases if case.automated_by}
            orphans = sorted(collected - bound)
            for nodeid in orphans[:20]:
                warnings.append(f"无用例归属的测试（黑户）: {nodeid}")
        else:
            warnings.append("未能收集到测试清单（被测项目或 pytest 不可用），跳过绑定校验")

        return errors, warnings

    # ------------------------------------------------------------ 覆盖率

    def coverage(self) -> dict:
        collected = collect_tests()
        bound = {_normalize(case.automated_by) for case in self.cases if case.automated_by}

        def _stat(group: list[Case]) -> dict:
            total = len(group)
            effective = [c for c in group if c.status == "automated" and _normalize(c.automated_by) in collected]
            return {
                "total": total,
                "automated": len(effective),
                "rate": round(len(effective) / total, 4) if total else 0.0,
            }

        by_layer = {layer: _stat([c for c in self.cases if c.layer == layer]) for layer in sorted(LAYERS)}
        by_priority = {pri: _stat([c for c in self.cases if c.priority == pri]) for pri in sorted(PRIORITIES)}
        return {
            "overall": _stat(self.cases),
            "by_layer": by_layer,
            "by_priority": by_priority,
            "collected_tests": len(collected),
            "bound_tests": len(bound & collected),
        }


def _normalize(nodeid: str) -> str:
    """把 `tests/a.py::test_x[1]` 归一化为 `tests/a.py::test_x`（参数化只算一条绑定）。"""
    nodeid = nodeid.strip()
    nodeid = re.sub(r"\[.*?\]$", "", nodeid)
    nodeid = nodeid.replace("\\", "/")
    nodeid = re.sub(r"^\./", "", nodeid)
    return nodeid


def collect_tests(lab_dir: Path | None = None) -> set[str]:
    """收集实验室里真实存在的测试 nodeid（含参数化展开后的基名）。"""
    target = lab_dir or LAB_DIR
    if not target.exists():
        return set()
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=target,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    nodeids: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("ERROR", "=", "_")):
            nodeids.add(_normalize(line))
    return nodeids


def mark_result(case: Case, result: str, when: str | None = None) -> None:
    case.last_run = when or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    case.last_result = result