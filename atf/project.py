"""项目注册表：让"这套流程"可以挂多个项目，而不是写死一个。

设计目标：**新增一个项目 = 加一段 YAML + 一个测试目录**，不动内核代码。

每个项目描述三件事：
- 流程配置（server / setup / suites / filing）
- 用例库（cases.yaml）
- 测试代码所在目录（suites_cwd，也是 `pytest --collect-only` 的基准）

兼容策略：未指定 `--project` 时取 `default`；旧的单项目命令仍然可用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "atf" / "projects.yaml"


class ProjectError(ValueError):
    """项目登记不合法：启动即失败，避免"跑到一半找不到项目"。"""


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    config: Path  # e2e 流程配置
    cases: Path  # 用例库
    suites_cwd: Path  # 测试代码目录
    description: str = ""

    @property
    def base_url(self) -> str:
        raw = yaml.safe_load(self.config.read_text(encoding="utf-8")) or {}
        server = raw.get("server") or {}
        return f"http://{server.get('host', '127.0.0.1')}:{server.get('port', 0)}"


@dataclass
class Registry:
    default: str
    projects: dict[str, Project]

    def get(self, project_id: str | None = None) -> Project:
        key = project_id or self.default
        if key not in self.projects:
            raise ProjectError(f"未登记的项目：{key}（可选：{', '.join(sorted(self.projects))}）")
        return self.projects[key]

    def all(self) -> list[Project]:
        return [self.projects[key] for key in sorted(self.projects)]


def load(path: Path | None = None) -> Registry:
    target = path or REGISTRY_PATH
    if not target.exists():
        raise ProjectError(f"缺少项目登记表：{target}")

    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    projects: dict[str, Project] = {}
    for project_id, item in (raw.get("projects") or {}).items():
        config = (ROOT / str(item.get("config", ""))).resolve()
        cases = (ROOT / str(item.get("cases", ""))).resolve()
        suites = (ROOT / str(item.get("suites_cwd", ""))).resolve()
        for label, value in (("config", config), ("cases", cases), ("suites_cwd", suites)):
            if not value.exists():
                raise ProjectError(f"项目 {project_id} 的 {label} 不存在：{value}")
        projects[project_id] = Project(
            id=project_id,
            name=str(item.get("name", project_id)),
            config=config,
            cases=cases,
            suites_cwd=suites,
            description=str(item.get("description", "")),
        )

    if not projects:
        raise ProjectError("项目登记表中没有任何项目")

    default = str(raw.get("default") or next(iter(projects)))
    if default not in projects:
        raise ProjectError(f"默认项目未登记：{default}")

    return Registry(default=default, projects=projects)