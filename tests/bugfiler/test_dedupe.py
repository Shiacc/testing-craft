"""去重逻辑测试：指纹、归一化、存储的读写与容错。

为什么指纹测试要单独成文件：
"重复提单"是自动化缺陷联动最容易翻车的地方——一旦指纹不稳定，每条失败都会新建一张单，
缺陷库被淹掉，团队很快就会把这个能力关掉。所以这里把三条不变式钉死：
    1. 同一处失败（只差噪声）→ 同指纹；
    2. 不同的失败（数字/用例不同）→ 不同指纹；
    3. 存储能跨实例读回，损坏时不静默丢数据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atf.bugfiler import BugDraft, BugFilerError, DedupeStore, fingerprint_for, normalize_actual
from atf.bugfiler import cli as bugfiler_cli

# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def store(tmp_path: Path) -> DedupeStore:
    return DedupeStore(tmp_path / "filed_bugs.json")


def _draft(**overrides: object) -> BugDraft:
    base: dict = {
        "title": "结算页满减券未生效",
        "description": "满 100 减 20 未生效",
        "case_id": "CAT-001",
        "test_id": "tests/test_checkout.py::test_coupon_applied",
        "actual": "AssertionError: payable 100.00 != 90.00",
    }
    base.update(overrides)
    return BugDraft(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 指纹


def test_default_fingerprint_is_stable() -> None:
    """同输入两次构造必须得到同一个指纹（默认指纹，不手工传）。"""
    first = _draft()
    second = _draft()

    assert first.fingerprint
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint == first.compute_fingerprint()
    assert len(first.fingerprint) == 64  # sha256 十六进制


def test_fingerprint_ignores_volatile_noise() -> None:
    """时间戳 / UUID / 耗时 / 端口 / 临时目录 变了，指纹必须不变。"""
    noisy_a = (
        "AssertionError: payable 100.00 != 90.00\n"
        "耗时 0.42s\n"
        "请求 id=3f2b8c9d-1a4e-4b7c-9d0e-1f2a3b4c5d6e 端口 :18080\n"
        "临时目录 C:\\Users\\admin\\AppData\\Local\\Temp\\pytest-abc123\n"
        "时间 2026-11-16 19:20:31"
    )
    noisy_b = (
        "AssertionError: payable 100.00 != 90.00\n"
        "耗时 0.57s\n"
        "请求 id=9c8b7a6f-5e4d-4c3b-8a21-0f9e8d7c6b5a 端口 :19090\n"
        "临时目录 C:\\Users\\admin\\AppData\\Local\\Temp\\pytest-zzz999\n"
        "时间 2026-11-17 08:01:02"
    )

    assert normalize_actual(noisy_a) == normalize_actual(noisy_b)
    assert _draft(actual=noisy_a).fingerprint == _draft(actual=noisy_b).fingerprint
    # 归一化只抹噪声，不动"错在哪"这部分语义
    assert "payable 100.00 != 90.00" in normalize_actual(noisy_a)


def test_fingerprint_distinguishes_real_differences() -> None:
    """不同 actual / 不同用例必须是不同指纹，否则会把两处缺陷合并成一条。"""
    base = _draft()

    assert _draft(actual="payable 100.00 != 90.00").fingerprint != _draft(actual="payable 100.00 != 80.00").fingerprint
    assert base.fingerprint != _draft(case_id="CAT-002").fingerprint
    assert base.fingerprint != _draft(test_id="tests/test_checkout.py::test_other").fingerprint
    assert fingerprint_for("CAT-001", "t", "a") != fingerprint_for("CAT-001", "t", "b")


def test_fingerprint_ignores_title_and_description() -> None:
    """改文案不该让去重失效：title/description 不参与指纹。"""
    assert _draft(title="原标题").fingerprint == _draft(title="改过的标题").fingerprint


# ---------------------------------------------------------------- 存储


def test_missing_fingerprint_returns_none(store: DedupeStore) -> None:
    assert store.check(fingerprint_for("CAT-001", "t", "a")) is None
    assert len(store) == 0


def test_record_then_check_hits(store: DedupeStore) -> None:
    """record 之后第二次 check 必须命中，且记录字段完整。"""
    fingerprint = _draft().fingerprint

    assert store.check(fingerprint) is None
    record = store.record(fingerprint, "zentao", "101", "http://zentao.local/bug-view-101.html")

    assert record["tracker"] == "zentao"
    assert record["key"] == "101"
    assert record["url"].endswith("bug-view-101.html")
    assert record["filed_at"]

    hit = store.check(fingerprint)
    assert hit == record
    assert fingerprint in store


def test_state_file_persists_across_instances(tmp_path: Path) -> None:
    """新建实例（模拟下一次 CI 运行）应能读到上一次的记录。"""
    path = tmp_path / "state" / "filed_bugs.json"
    fingerprint = _draft().fingerprint

    DedupeStore(path).record(fingerprint, "jira", "QA-9", "https://jira.local/browse/QA-9")

    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert fingerprint in payload
    assert DedupeStore(path).check(fingerprint)["key"] == "QA-9"


def test_record_overwrites_same_fingerprint(store: DedupeStore) -> None:
    """同一指纹重提（--force）时以最新一条为准。"""
    fingerprint = _draft().fingerprint
    store.record(fingerprint, "zentao", "101", "u1")
    store.record(fingerprint, "zentao", "202", "u2")

    assert store.check(fingerprint)["key"] == "202"
    assert len(store) == 1


def test_check_returns_copy(store: DedupeStore) -> None:
    """调用方改返回值不该污染磁盘上的真相。"""
    fingerprint = _draft().fingerprint
    store.record(fingerprint, "zentao", "101", "u1")

    hit = store.check(fingerprint)
    assert hit is not None
    hit["key"] = "被调用方改坏"

    assert store.check(fingerprint)["key"] == "101"


def test_forget_removes_entry(store: DedupeStore) -> None:
    fingerprint = _draft().fingerprint
    store.record(fingerprint, "zentao", "101", "u1")

    assert store.forget(fingerprint) is True
    assert store.check(fingerprint) is None
    assert store.forget(fingerprint) is False


def test_empty_fingerprint_is_rejected(store: DedupeStore) -> None:
    with pytest.raises(BugFilerError):
        store.check("")
    with pytest.raises(BugFilerError):
        store.record("", "zentao", "1", "u")


def test_corrupt_state_file_is_quarantined(tmp_path: Path) -> None:
    """半截写入 / 手工改坏时：备份坏文件、当作空表继续，不阻断这次提单。"""
    path = tmp_path / "filed_bugs.json"
    path.write_text('{"abc": {"tracker": "zentao"', encoding="utf-8")
    store = DedupeStore(path)

    assert store.check("abc") is None
    backups = list(tmp_path.glob("filed_bugs.json.corrupt-*"))
    assert len(backups) == 1

    store.record("abc", "zentao", "1", "u")
    assert json.loads(path.read_text(encoding="utf-8"))["abc"]["key"] == "1"


def test_strict_mode_raises_on_corrupt_file(tmp_path: Path) -> None:
    """strict=True 供"宁可红也不丢状态"的场景使用。"""
    path = tmp_path / "filed_bugs.json"
    path.write_text("not json at all", encoding="utf-8")

    with pytest.raises(BugFilerError):
        DedupeStore(path, strict=True).check("abc")


def test_non_dict_entries_are_ignored(tmp_path: Path) -> None:
    """手工编辑可能留下字符串值，别让它在调用方炸成 AttributeError。"""
    path = tmp_path / "filed_bugs.json"
    path.write_text(json.dumps({"good": {"tracker": "jira", "key": "QA-1"}, "bad": "oops"}), encoding="utf-8")

    store = DedupeStore(path)
    assert len(store) == 1
    assert store.check("bad") is None


# ---------------------------------------------------------------- 草稿校验


def test_blank_fields_are_rejected() -> None:
    with pytest.raises(BugFilerError):
        BugDraft(title="   ", description="有描述")
    with pytest.raises(BugFilerError):
        BugDraft(title="有标题", description="  ")


def test_long_title_is_clipped() -> None:
    """缺陷系统的标题长度有限，构造期就截断，而不是等接口 400。"""
    draft = _draft(title="很长" * 100)

    assert len(draft.title) <= 120
    assert draft.title.endswith("…")


def test_priority_is_clamped_and_severity_validated() -> None:
    assert _draft(priority=9).priority == 4
    assert _draft(priority=0).priority == 1
    with pytest.raises(BugFilerError):
        _draft(severity="紧急")


# ---------------------------------------------------------------- CLI


def test_cli_dedupe_check_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """0 = 未提过（可以提），1 = 已存在记录。"""
    state = str(tmp_path / "filed_bugs.json")

    assert bugfiler_cli.main(["dedupe-check", "--case-id", "CAT-001", "--actual", "boom", "--state", state]) == 0
    assert "未提交过" in capsys.readouterr().out

    fingerprint = fingerprint_for("CAT-001", "", "boom")
    DedupeStore(Path(state)).record(fingerprint, "zentao", "101", "http://zentao.local/bug-view-101.html")

    assert bugfiler_cli.main(["dedupe-check", "--case-id", "CAT-001", "--actual", "boom", "--state", state]) == 1
    assert "已存在" in capsys.readouterr().out
