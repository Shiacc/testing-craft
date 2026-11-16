"""缺陷草稿（BugDraft）：把"一次自动化测试失败"翻译成缺陷系统能接受的单据。

为什么需要这层中间模型，而不是让测试直接调禅道 / Jira 的 HTTP 接口：

1. **失败现场与缺陷字段是两套语言。** pytest 那边有的是 nodeid、断言消息、环境名；
   禅道要 product/title/severity/steps/pri，Jira 要 project/summary/issuetype + ADF。
   没有中间层，每接一个新缺陷系统都要在测试代码里重写一遍拼装逻辑。
2. **指纹必须与缺陷系统无关。** 同一个缺陷无论提给禅道还是 Jira，都该命中同一条去重
   记录；"这是不是同一个缺陷"的判断只有落在中间层，才不会被某个系统的字段口径绑架。
3. **校验在构造期完成。** 缺陷系统对 title 长度、severity 取值有硬限制；让错误在
   BugDraft 构造时就暴露，比等 CI 里提单失败、再翻 300 行日志便宜得多。

关于指纹为什么要**归一化**（见 ``normalize_actual``）：断言消息里天然带着每次都会变的
东西——时间戳、UUID、临时目录、耗时、端口号、十六进制句柄。直接对原始 actual 做 sha256，
同一处失败每跑一次就是一条新指纹，去重形同虚设，最后缺陷库里堆满同一个 bug 的几十张单。
归一化把这些"会变的噪声"替换成占位符，只留下"失败长什么样"这一不变特征。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# 禅道口径：severity 用中文四档描述，adapter 负责翻译成各系统的数字/枚举。
SEVERITIES: tuple[str, ...] = ("致命", "严重", "一般", "轻微")
PRIORITY_MIN, PRIORITY_MAX = 1, 4
TITLE_LIMIT = 120


class BugFilerError(RuntimeError):
    """缺陷联动链路上的可预期失败：参数不合法、认证失败、缺陷系统 4xx/5xx、网络超时。

    为什么单独造一个异常类型，而不是把 httpx 的异常直接抛给上层：
    主流程只关心"这次没提上去、要不要重试"，不该被迫了解 httpx 的异常层级。把状态码与
    响应片段挂在异常上，日志里就能直接看到缺陷系统到底说了什么（片段已脱敏，见 mask_secret）。
    """

    def __init__(self, message: str, *, status_code: int | None = None, snippet: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.snippet = snippet


# ---------------------------------------------------------------- 工具函数

# 这些正则只服务于一件事：把"每次运行都会变、但与缺陷本质无关"的片段抹平。
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?\b"
)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_HEX_RE = re.compile(r"\b0x[0-9a-f]{4,}\b|\b[0-9a-f]{16,}\b", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:ms|millis|s|sec|secs|seconds?|minutes?)\b", re.IGNORECASE
)
_PORT_RE = re.compile(r"(?<=:)\d{2,5}\b")
_TMP_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/tmp/|/var/folders/)[^\s\"'<>]*")
_SPACE_RE = re.compile(r"\s+")


def normalize_actual(actual: str) -> str:
    """把断言消息归一化成"可与历史失败比对"的形式。

    归一化刻意只做**低风险替换**：时间戳、UUID、长十六进制串、耗时、端口、临时路径。
    不做通用数字替换（比如把 90.00 抹成 <num>）——那会把"期望 90 实得 100"和
    "期望 90 实得 200"合并成同一个缺陷，属于危险去重，宁可有少量漏合并。
    最后统一小写并压缩空白，抵消 pytest 输出里的缩进与换行差异。
    """
    text = "" if actual is None else str(actual)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TIMESTAMP_RE.sub("<ts>", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _DURATION_RE.sub("<dur>", text)
    text = _PORT_RE.sub("<port>", text)
    text = _TMP_PATH_RE.sub("<path>", text)
    text = _SPACE_RE.sub(" ", text)
    return text.strip().lower()


def mask_secret(value: str, *, visible: int = 4) -> str:
    """把 token / 密码变成可安全写进日志的形式：``***abcd``。

    缺陷联动一定会被塞进 CI 日志，而 token 就是在这条链路上流动的。
    与其要求每个调用方"记得别打印"，不如在模型层给出唯一一个可见字符串的入口。
    """
    text = (value or "").strip()
    if not text:
        return "(未设置)"
    if len(text) <= visible:
        return "***"
    return f"***{text[-visible:]}"


def clip(text: str, limit: int) -> str:
    """按**字符**截断（不是字节），超出时用省略号收尾，保证不超过 limit。"""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def fingerprint_for(case_id: str, test_id: str, actual: str) -> str:
    """不复用 BugDraft 也能算指纹（``dedupe-check`` 这类只查询的场景用）。

    逻辑与 ``BugDraft.compute_fingerprint`` 完全一致——这是刻意的：指纹算法只有一个实现，
    草稿只是它的调用方。两处各写一遍的话，"查重"与"记录"迟早会算出两个不同的值。
    """
    material = f"{(case_id or '').strip()}|{(test_id or '').strip()}|{normalize_actual(actual)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 数据模型


@dataclass
class BugDraft:
    """一张待提交的缺陷单。字段口径对齐主流程，与具体缺陷系统无关。"""

    title: str
    description: str
    case_id: str = ""
    test_id: str = ""
    severity: str = "一般"
    priority: int = 3
    environment: str = ""
    steps: list[str] = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    attachments: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    fingerprint: str = ""

    def __post_init__(self) -> None:
        # 就地规整：调用方从 pytest 报告里捞出来的字符串常带缩进与换行。
        self.title = clip(self.title, TITLE_LIMIT)
        self.description = (self.description or "").strip()
        self.case_id = (self.case_id or "").strip()
        self.test_id = (self.test_id or "").strip()
        self.environment = (self.environment or "").strip()
        self.expected = (self.expected or "").strip()
        self.actual = (self.actual or "").strip()
        self.severity = (self.severity or "").strip()
        self.steps = [str(step).strip() for step in (self.steps or []) if str(step).strip()]
        self.attachments = [str(path).strip() for path in (self.attachments or []) if str(path).strip()]
        self.labels = [str(label).strip() for label in (self.labels or []) if str(label).strip()]

        if not self.title:
            raise BugFilerError("BugDraft.title 不能为空（它会成为缺陷标题）")
        if not self.description:
            raise BugFilerError("BugDraft.description 不能为空（缺陷系统会拒绝空描述）")
        if self.severity not in SEVERITIES:
            raise BugFilerError(f"BugDraft.severity 必须是 {'/'.join(SEVERITIES)}，收到 {self.severity!r}")
        try:
            priority = int(self.priority)
        except (TypeError, ValueError) as exc:
            raise BugFilerError(f"BugDraft.priority 必须是 {PRIORITY_MIN}-{PRIORITY_MAX} 的整数") from exc
        # 越界不报错而是夹到合法区间：优先级是"排序提示"，夹一下比让整条提单链路失败划算。
        self.priority = min(PRIORITY_MAX, max(PRIORITY_MIN, priority))

        if not self.fingerprint:
            self.fingerprint = self.compute_fingerprint()

    def compute_fingerprint(self) -> str:
        """sha256(case_id | test_id | 归一化后的 actual)。

        为什么不把 title / description 也算进去：这两者是可编辑的文案，改一句话不该让
        去重失效；而 case_id + test_id 定位"哪条用例"，归一化的 actual 定位"错在哪"，
        两者组合已经能唯一标识一处失败。
        """
        return fingerprint_for(self.case_id, self.test_id, self.actual)

    # ------------------------------------------------------------ 呈现

    def steps_text(self) -> str:
        """复现步骤拼成多行文本（禅道 steps 字段与 Jira 的 codeBlock 共用）。"""
        if not self.steps:
            return "（自动化测试直接失败，无人工复现步骤）"
        return "\n".join(f"{index}. {step}" for index, step in enumerate(self.steps, start=1))

    def trace_text(self) -> str:
        """定位信息单行摘要：谁、在哪跑、什么严重度。两个 adapter 的描述头都用它。"""
        parts: list[str] = []
        if self.case_id:
            parts.append(f"用例 {self.case_id}")
        if self.test_id:
            parts.append(f"自动化 {self.test_id}")
        if self.environment:
            parts.append(f"环境 {self.environment}")
        parts.append(f"严重度 {self.severity}")
        parts.append(f"优先级 {self.priority}")
        if self.labels:
            parts.append(f"标签 {', '.join(self.labels)}")
        return "；".join(parts)

    def replay_text(self) -> str:
        """把失败现场（期望 / 实际）压成文本块，供缺陷单的正文使用。"""
        blocks: list[str] = []
        if self.expected:
            blocks.append(f"【期望】\n{self.expected}")
        if self.actual:
            blocks.append(f"【实际】\n{self.actual}")
        if not blocks:
            blocks.append("【实际】\n（未记录断言差异）")
        return "\n\n".join(blocks)

    def as_dict(self) -> dict:
        """结构化导出（不含任何凭据；BugDraft 本身也不该携带 token）。"""
        return {
            "title": self.title,
            "case_id": self.case_id,
            "test_id": self.test_id,
            "severity": self.severity,
            "priority": self.priority,
            "environment": self.environment,
            "fingerprint": self.fingerprint,
            "labels": list(self.labels),
        }
