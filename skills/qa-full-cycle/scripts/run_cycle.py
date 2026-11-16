#!/usr/bin/env python3
"""qa-full-cycle 一键执行。

流程：环境体检 → 探测 atf → 执行 `python -m atf run ...` → 汇报结果与下一步。
只用标准库 + subprocess；默认不落盘（--record 才写一份运行记录）。

用法：
    python skills/qa-full-cycle/scripts/run_cycle.py --dry-run --tag smoke
    python skills/qa-full-cycle/scripts/run_cycle.py --tag smoke --shards 2
    python skills/qa-full-cycle/scripts/run_cycle.py --skip-env-check --tag p0
    python skills/qa-full-cycle/scripts/run_cycle.py --tag smoke --record runs/smoke.json

退出码：
    0   执行成功（全绿）
    1   有用例失败（atf 非 0 退出，原样透传为 1）
    2   环境体检未通过（先修环境，不要跳过）
    3   atf 不可用（未安装到当前解释器 / 不可调用），脚本会打印三条替代路径
    64  参数错误
    130 用户中断（Ctrl+C）

约定：
    * 本仓库只含知识文档与 labs 实验，**不含 atf 实现**，所以默认环境下本脚本会以退出码 3 结束。
      这是预期行为，不是 bug：它阻止你在"看起来跑过了"的假象上继续往下走。
    * 失败必须能追溯到用例 ID；本脚本只负责执行与取回退出码，归因在 references/workflow.md S4。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CHECK_ENV = SCRIPT_DIR / "check_env.py"
SKILL_DIR = SCRIPT_DIR.parent

EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_ENV_FAILED = 2
EXIT_ATF_MISSING = 3


class Parser(argparse.ArgumentParser):
    """参数错误统一 64，避免与"体检未通过"的 2 混淆。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"参数错误: {message}\n")
        raise SystemExit(64)


def _run(cmd: list[str], timeout: int = 180) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return 127, f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    except OSError as exc:
        return 126, str(exc)
    return proc.returncode, f"{proc.stdout or ''}{proc.stderr or ''}".strip()


def _first_line(text: str, limit: int = 100) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def probe_atf(python_exe: str) -> tuple[bool, str]:
    """返回 (是否可用, 说明)。说明只留一句人话，不把解释器的整段堆栈塞进输出。"""
    code, out = _run([python_exe, "-m", "atf", "--help"], timeout=120)
    if code == 0:
        return True, _first_line(out) or "可调用"
    lowered = out.lower()
    if "no module named" in lowered:
        return False, "未安装：当前解释器找不到 atf 模块"
    if code == 124:
        return False, "调用超时：python -m atf --help 无响应"
    return False, f"不可调用：{_first_line(out) or f'退出码 {code}'}"


def explain_atf_missing(python_exe: str, detail: str) -> None:
    print()
    print("=" * 72)
    print("[qa-full-cycle] atf 不可用，流程无法继续执行  退出码 3")
    print("=" * 72)
    print(f"解释器: {python_exe}")
    print(f"探测命令: {python_exe} -m atf --help")
    print(f"输出: {detail}")
    print()
    print("为什么是预期行为：atf 是本流程的执行入口（分层选择、分片、JUnit 输出）。")
    print("本仓库（testing-craft）只含知识文档与 labs 实验，不含 atf 实现，")
    print("所以未安装时这条命令必然失败——不要把它当成脚本缺陷去改退出码。")
    print()
    print("三条路，按推荐顺序：")
    print("  1) 安装 atf（推荐）")
    print("     python -m pip install -e <atf 仓库路径>")
    print("     然后自检: python -m atf --help")
    print("  2) 临时指向源码（不安装）")
    print("     Windows: set PYTHONPATH=<atf 源码根目录>")
    print("     macOS/Linux: export PYTHONPATH=<atf 源码根目录>")
    print("     然后重跑本脚本")
    print("  3) 用 pytest 顶上（本轮可交付，但报告里必须注明“未经 atf”）")
    print("     python -m pytest -q --junitxml=runs/junit.xml")
    print("     随后人工把失败 nodeid 映射回用例 ID —— 见 references/workflow.md S3「失败回退」")
    print()
    print(f"文档: {SKILL_DIR / 'references' / 'workflow.md'}")


def build_atf_command(python_exe: str, args: argparse.Namespace) -> list[str]:
    cmd = [python_exe, "-m", "atf", "run"]
    if args.tag:
        cmd += ["--tag", args.tag]
    if args.shards:
        cmd += ["--shards", str(args.shards)]
    if args.target_url:
        cmd += ["--target-url", args.target_url]
    extras = [item for item in args.extra if item != "--"]
    cmd += extras
    return cmd


def write_record(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = Parser(
        prog="run_cycle.py",
        description="qa-full-cycle 一键执行：环境体检 → 探测 atf → python -m atf run → 汇报与下一步",
        epilog=(
            "示例:\n"
            "  python run_cycle.py --dry-run --tag smoke      # 只看会执行什么\n"
            "  python run_cycle.py --tag smoke --shards 2     # 真跑\n"
            "  python run_cycle.py --skip-env-check --tag p0  # 跳过体检（自担风险）\n"
            "  python run_cycle.py --tag smoke -- --keyword cart\n"
            "退出码: 0 全绿 / 1 有用例失败 / 2 体检未通过 / 3 atf 不可用 / 64 参数错误 / 130 中断"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tag", help="用例标签选择（作为 --tag 传给 atf），如 smoke / p0 / regression")
    parser.add_argument("--shards", type=int, help="分片数（作为 --shards 传给 atf），本地小规模建议 1-2")
    parser.add_argument("--target-url", help="被测环境地址（作为 --target-url 传给 atf，并用于体检）")
    parser.add_argument("--python", default=sys.executable, help="执行 atf 的解释器，默认当前解释器")
    parser.add_argument("--skip-env-check", action="store_true", help="跳过 check_env.py 体检（不推荐）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令，不真正执行")
    parser.add_argument("--record", help="把本次运行摘要写到该 JSON 路径（默认不落盘）")
    parser.add_argument("extra", nargs=argparse.REMAINDER, help="`--` 之后的参数原样透传给 atf run")
    args = parser.parse_args(argv)

    if args.shards is not None and args.shards < 1:
        parser.error("--shards 必须 ≥ 1")

    python_exe = args.python
    if shutil.which(python_exe) is None and not Path(python_exe).exists():
        print(f"[qa-full-cycle] 解释器不存在: {python_exe}", file=sys.stderr)
        return 64

    started = time.time()
    started_at = datetime.now().isoformat(timespec="seconds")
    print("=" * 72)
    print("qa-full-cycle · 一键执行")
    print(f"解释器: {python_exe}")
    print(f"开始时间: {started_at}")
    print("=" * 72)

    # ---------- 1) 环境体检 ----------
    if args.skip_env_check:
        print("\n[1/3] 环境体检：已跳过（--skip-env-check）")
    else:
        print("\n[1/3] 环境体检（scripts/check_env.py）")
        env_cmd = [python_exe, str(CHECK_ENV)]
        if args.target_url:
            env_cmd += ["--target-url", args.target_url, "--require-target"]
        env_code = subprocess.call(env_cmd)
        if env_code != 0:
            print()
            print(f"[qa-full-cycle] 环境体检未通过（退出码 {env_code}），停止执行  退出码 2")
            print("先修 FAIL 项；确实要先跑可以加 --skip-env-check，但结论里必须写明体检被跳过。")
            return EXIT_ENV_FAILED

    # ---------- 2) 探测 atf ----------
    print("\n[2/3] 探测 atf")
    available, detail = probe_atf(python_exe)
    if available:
        print(f"  atf 可调用: {detail}")
    else:
        print(f"  atf 不可用: {detail}")

    # ---------- 3) 执行 ----------
    cmd = build_atf_command(python_exe, args)
    quoted = " ".join(cmd)
    print("\n[3/3] 执行")
    print(f"  $ {quoted}")
    if not available:
        # 先打印命令再解释：让使用者看到"本来要跑什么"，同时不允许假装跑过
        explain_atf_missing(python_exe, detail)
        return EXIT_ATF_MISSING
    if args.dry_run:
        print("\n[dry-run] 未执行。确认命令无误后去掉 --dry-run 重跑。")
        print("  注意：dry-run 不产生任何测试结论，不得用它声称“已执行”。")
        if args.record:
            write_record(
                Path(args.record),
                {
                    "started_at": started_at,
                    "dry_run": True,
                    "command": quoted,
                    "tag": args.tag,
                    "shards": args.shards,
                    "target_url": args.target_url,
                },
            )
            print(f"  运行摘要已写入: {args.record}")
        return EXIT_OK

    try:
        code = subprocess.call(cmd)
    except KeyboardInterrupt:
        print("\n[qa-full-cycle] 已中断（退出码 130）。注意：中断不等于通过，结果文件可能不完整。")
        return 130

    duration = time.time() - started
    print()
    print("-" * 72)
    if code == 0:
        print(f"结论: 执行成功（atf 退出码 0），耗时 {duration:.1f}s")
        print("下一步: 把结果登记回用例库（last_run / last_result），并抽 2 条连跑两次确认确定性。")
        exit_code = EXIT_OK
    else:
        print(f"结论: 有用例失败（atf 退出码 {code}），耗时 {duration:.1f}s")
        print("下一步:")
        print("  1) 每条失败先归因（产品/用例/环境/数据/flaky），见 references/workflow.md S4")
        print("  2) 失败必须能落到用例 ID；映射不上的先补绑定，不许报无名失败")
        print("  3) 提缺陷先出 dry-run：references/bug-integration.md §6")
        print("  不许为了让面板变绿去改基线或改断言（SKILL.md 铁律 3）")
        exit_code = EXIT_TESTS_FAILED

    if args.record:
        write_record(
            Path(args.record),
            {
                "started_at": started_at,
                "duration_s": round(duration, 2),
                "dry_run": False,
                "command": quoted,
                "atf_exit_code": code,
                "exit_code": exit_code,
                "tag": args.tag,
                "shards": args.shards,
                "target_url": args.target_url,
            },
        )
        print(f"运行摘要已写入: {args.record}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())