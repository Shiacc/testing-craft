"""生成 docs/index.md：文档与实验的索引页。

为什么用脚本生成而不是手写：
文档一旦超过 10 篇，手写索引必然与正文漂移（新增一章忘了加、改标题忘了同步）。
这里从每篇文档的**头部元信息**（适用读者 / 前置阅读 / 配套实验）与章节结构自动提取，
`--check` 模式可直接进 CI：索引过期就让流水线红。

用法：
    python scripts/build_docs_index.py            # 生成/更新 docs/index.md
    python scripts/build_docs_index.py --check    # 只检查是否最新（CI 用）
    python scripts/build_docs_index.py --print    # 输出到 stdout，不写文件
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
LABS = ROOT / "labs"
INDEX = DOCS / "index.md"

META_RE = re.compile(r"\*\*(适用读者|前置阅读|配套实验)\*\*：(.+?)(?=　\||$)")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")

# 反向索引：症状 → 该看哪篇。刻意手写——"问题"这一侧无法从正文可靠推导。
REVERSE_INDEX: list[tuple[str, str, str]] = [
    ("不知道要不要写自动化、写了值不值", "02", "02-when-to-automate.md"),
    ("UI 用例越写越多、越来越慢", "02 §6、07 §6", "02-when-to-automate.md"),
    ("团队在选框架 / 选平台，需要可解释的结论", "05", "05-selection-methodology.md"),
    ("想上测试平台，但说不清该不该上", "04 §7", "04-test-platforms.md"),
    ("接口测试 / 契约测试怎么写、用什么工具", "03 §3、03 §6", "03-test-frameworks.md"),
    ("E2E 老是 flaky，不知道该点哪里", "07 §9", "07-engineering-and-ci.md"),
    ("测试数据互相污染、环境不一致", "07 §4、07 §5", "07-engineering-and-ci.md"),
    ("流水线太慢，想知道怎么分片并行", "07 §7", "07-engineering-and-ci.md"),
    ("要设计门禁规则、担心被绕过", "08 §5、08 §6", "08-metrics-and-quality-gates.md"),
    ("需要向管理层证明测试投入的价值", "08 §2、08 §3", "08-metrics-and-quality-gates.md"),
    ("用例没人维护、一年后大面积失效", "09 §3、09 §4", "09-organization-and-governance.md"),
    ("要写用例评审清单 / 选型评分表 / RFP", "11", "11-templates-and-checklists.md"),
    ("团队好像有一堆问题，但说不清是哪类", "10", "10-anti-patterns.md"),
    ("想按团队规模/行业找现成配方", "06", "06-scenario-playbook.md"),
    ("概念不统一，评审会各说各话", "01", "01-concepts-and-terminology.md"),
]

LAB_LAYERS = {
    "01-unit-pytest": "L1 单元",
    "02-api-contract": "L3 接口/契约",
    "03-ui-playwright": "L4 E2E",
    "04-perf": "L5 性能",
    "05-ci": "流程层",
    "06-platform-lite": "平台层",
}

LAB_COMMANDS = {
    "01-unit-pytest": "pytest -q",
    "02-api-contract": "pytest -q",
    "03-ui-playwright": "pytest -q",
    "04-perf": "python py_load_smoke.py",
    "05-ci": "python scripts/ci_local.py --shards 2 --with-perf",
    "06-platform-lite": "python -m platform_lite run --tag smoke",
}


@dataclass
class Doc:
    number: str
    title: str
    path: str
    audience: str = ""
    prereq: str = ""
    lab: str = ""
    summary: str = ""
    sections: list[str] = field(default_factory=list)
    lines: int = 0


@dataclass
class Lab:
    number: str
    title: str
    path: str
    one_liner: str = ""
    layer: str = ""
    command: str = ""


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接保留文案
    text = text.replace("**", "").replace("`", "")
    return text.strip(" 　|")


def parse_doc(path: Path) -> Doc:
    lines = path.read_text(encoding="utf-8").splitlines()
    title = ""
    meta: dict[str, str] = {}
    summary_parts: list[str] = []
    sections: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not title:
            match = TITLE_RE.match(stripped)
            if match:
                title = match.group(1)
                continue
        if stripped.startswith(">"):
            body = stripped.lstrip("> ").strip()
            if not body:
                continue
            found = META_RE.findall(body)
            if found:
                for key, value in found:
                    meta[key] = _strip_markdown(value)
                continue
            if not summary_parts:
                summary_parts.append(_strip_markdown(body))
            continue
        match = SECTION_RE.match(stripped)
        if match:
            sections.append(match.group(1))

    summary = summary_parts[0] if summary_parts else ""
    if len(summary) > 64:
        summary = summary[:62].rstrip() + "…"

    return Doc(
        number=path.name.split("-")[0],
        title=title or path.stem,
        path=path.name,
        audience=meta.get("适用读者", ""),
        prereq=meta.get("前置阅读", ""),
        lab=meta.get("配套实验", ""),
        summary=summary,
        sections=sections,
        lines=len(lines),
    )


def parse_lab(path: Path) -> Lab:
    lines = path.read_text(encoding="utf-8").splitlines()
    title = ""
    one_liner = ""
    for line in lines:
        stripped = line.strip()
        if not title:
            match = TITLE_RE.match(stripped)
            if match:
                title = match.group(1)
                continue
        if stripped.startswith(">") and not one_liner:
            body = stripped.lstrip("> ").strip()
            if body:
                one_liner = _strip_markdown(body).removeprefix("一句话：")
    name = path.parent.name
    if len(one_liner) > 56:
        one_liner = one_liner[:54].rstrip() + "…"
    return Lab(
        number=name.split("-")[0],
        title=title or name,
        path=f"../labs/{name}/README.md",
        one_liner=one_liner,
        layer=LAB_LAYERS.get(name, ""),
        command=LAB_COMMANDS.get(name, ""),
    )


def collect() -> tuple[list[Doc], list[Lab]]:
    docs = [
        parse_doc(p)
        for p in sorted(DOCS.glob("*.md"))
        if p.name not in ("README.md", "index.md")
    ]
    labs = [parse_lab(p) for p in sorted(LABS.glob("*/README.md"))]
    return docs, labs


def _display_title(doc: Doc) -> str:
    """去掉标题里重复的编号前缀（`01 · 概念与术语体系` → `概念与术语体系`）。"""
    return re.sub(r"^\d+\s*·\s*", "", doc.title)


def render(docs: list[Doc], labs: list[Lab]) -> str:
    total_lines = sum(doc.lines for doc in docs)
    total_sections = sum(len(doc.sections) for doc in docs)

    out: list[str] = [
        "# 文档索引 · Testing Craft",
        "",
        "> **本页由脚本生成，请勿手工编辑。** 修改文档头部元信息后运行：",
        "> `python scripts/build_docs_index.py`（CI 可用 `--check` 校验是否过期）。",
        "",
        f"- 文档 **{len(docs)}** 篇，合计 **{total_lines}** 行、**{total_sections}** 个小节",
        f"- 配套实验 **{len(labs)}** 个（全部可运行，验证结果见 [仓库首页](../README.md#实验验证状态)）",
        "- 版本：v1.0.0　|　生成时间：见文件末尾",
        "",
        "---",
        "",
        "## 1. 全部文档",
        "",
        "| # | 文档 | 一句话 | 适用读者 | 小节 | 行数 |",
        "|---|---|---|---|---|---|",
    ]

    for doc in docs:
        out.append(
            f"| {doc.number} | [{_display_title(doc)}]({doc.path}) | {doc.summary} | "
            f"{doc.audience or '全员'} | {len(doc.sections)} | {doc.lines} |"
        )

    out += [
        "",
        "> 注意：03 / 04 / 05 需要先读 01；02 是几乎所有后续章节的前置。",
        "",
        "---",
        "",
        "## 2. 按问题查文档（反向索引）",
        "",
        "不知道看哪篇时，从「症状」入手最快。",
        "",
        "| 你遇到的现象 | 去看 | 直接跳转 |",
        "|---|---|---|",
    ]
    for symptom, where, filename in REVERSE_INDEX:
        out.append(f"| {symptom} | {where} | [{filename}]({filename}) |")

    out += [
        "",
        "---",
        "",
        "## 3. 按角色阅读路径",
        "",
        "| 角色 | 顺序 | 预计耗时 |",
        "|---|---|---|",
        "| 研发管理者 / 决策者 | 01 → 02 → 06 → 08 → 09 | 约 40 分钟 |",
        "| 测试架构师 / QA 负责人 | 01 → 02 → 03 → 04 → 05 → 07 → 11 | 约 2 小时 |",
        "| 一线工程师 | 01 §3-4 → 03 → 跑 labs → 07 → 10 | 边做边读 |",
        "| 正在踩坑、需要急救 | 10（按症状定位）→ 对应章节的修复路径 | 15 分钟 |",
        "",
        "---",
        "",
        "## 4. 每篇包含哪些小节",
        "",
        "用于快速判断「这篇里面有没有我要的那一段」。",
        "",
    ]
    for doc in docs:
        sections = " · ".join(doc.sections) if doc.sections else "（无二级小节）"
        out.append(f"- **{doc.number} {_display_title(doc)}** — {sections}")

    out += [
        "",
        "---",
        "",
        "## 5. 配套实验",
        "",
        "| # | 实验 | 层级 | 一句话 | 运行 |",
        "|---|---|---|---|---|",
    ]
    for lab in labs:
        command = f"`{lab.command}`" if lab.command else ""
        out.append(f"| {lab.number} | [{lab.title}]({lab.path}) | {lab.layer} | {lab.one_liner} | {command} |")

    out += [
        "",
        "> 实验之间互不依赖，可单独 clone 后运行；全部实测通过（见首页验证表）。",
        "",
        "---",
        "",
        "## 6. 维护约定",
        "",
        "| 场景 | 动作 |",
        "|---|---|",
        "| 新增一篇文档 | 文件命名为 `NN-kebab-case.md`，头部补齐「适用读者 / 前置阅读」，然后重跑本脚本 |",
        "| 改标题 | 只改 `# 标题`，索引自动跟随 |",
        "| 新增小节 | 无需手动维护索引，`--check` 会提示重建 |",
        "| 新增加反向索引条目 | 在 `scripts/build_docs_index.py` 的 `REVERSE_INDEX` 里加一行 |",
        "",
        "---",
        "",
        f"*索引生成于 {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}，"
        f"由 `scripts/build_docs_index.py` 自动产出。*",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 docs/index.md 文档索引页")
    parser.add_argument("--check", action="store_true", help="只校验索引是否最新（CI 用）")
    parser.add_argument("--print", dest="to_stdout", action="store_true", help="输出到 stdout")
    args = parser.parse_args(argv)

    docs, labs = collect()
    content = render(docs, labs)

    if args.to_stdout:
        print(content)
        return 0

    if args.check:
        current = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
        # 忽略时间戳行，避免每次生成都被判为过期
        normalise = lambda text: re.sub(r"索引生成于 .*?，", "索引生成于 <time>，", text)
        if normalise(current) != normalise(content):
            print("[索引过期] docs/index.md 与正文不一致，请运行：python scripts/build_docs_index.py")
            return 1
        print("[索引最新] docs/index.md 与正文一致")
        return 0

    INDEX.write_text(content, encoding="utf-8", newline="\n")
    print(f"[索引已生成] {INDEX}（{len(docs)} 篇文档、{len(labs)} 个实验）")
    return 0


if __name__ == "__main__":
    sys.exit(main())