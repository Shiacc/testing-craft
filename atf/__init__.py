"""atf —— 自动化测试框架层（Automation Test Framework）。

这一层解决的问题：**用例库（人写的规格）与测试代码（机器执行）之间的双向绑定**，
以及"失败 → 归因 → 提缺陷"的自动化闭环。

模块：
- catalog   用例库：加载/校验/覆盖率/孤儿检测
- runner    执行：跑 pytest、解析 JUnit、回写用例结果
- bugflow   缺陷流：失败项 → BugDraft → 指纹去重 → 提交禅道/Jira
- report    报告：summary.md + results.json
- cli       统一入口：validate / coverage / run / file-bugs

与 labs/06-platform-lite 的关系：那个是"平台原型"（调度与门禁），
本模块是"框架层"（用例与缺陷资产），二者可以组合使用。
"""

__version__ = "0.1.0"