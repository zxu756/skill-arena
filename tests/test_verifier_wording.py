"""M2 Stage 2 tests for self-report wording audit: 10 keywords, same-item evidence, and 4 denoising rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_arena.cli import main


def test_self_report_missing_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--lang", "zh", "verify", "--self-report", str(tmp_path / "missing-report.md")])
    cap = capsys.readouterr()
    assert rc == 2
    assert "不存在" in (cap.out + cap.err) or "exist" in (cap.out + cap.err).lower()


def test_self_report_all_ten_keywords_and_same_item_evidence(tmp_path: Path) -> None:
    # Unsupported report hitting all 10 keywords across paragraph, bullet, and table row
    bad_md = tmp_path / "bad_report.md"
    bad_md.write_text(
        "# M0 自报\n\n"
        "- 已彻底消除残留进程，全部问题已修，绝不漏报，无任何跳过。\n"
        "- 覆盖率达到 100%，全仓零警告。\n"
        "- It never leaks files, makes zero network calls, and is completely isolated.\n\n"
        "| 行 | 说明 |\n"
        "|---|---|\n"
        "| 1 | 全部断言均通过（仅文字说明，未附命令与退出码） |\n",
        encoding="utf-8",
    )
    j_bad = tmp_path / "bad.json"
    m_bad = tmp_path / "bad.md"
    rc_bad = main(
        ["--lang", "zh", "verify", "--self-report", str(bad_md), "--json-out", str(j_bad), "--md-out", str(m_bad)]
    )
    assert rc_bad == 1
    d_bad = json.loads(j_bad.read_text(encoding="utf-8"))
    md_text = m_bad.read_text(encoding="utf-8")
    assert d_bad["conclusion"] == "FAIL"
    sr = d_bad["self_report"]
    assert sr["enabled"] is True
    assert sr["status"] == "FAIL"

    flagged = [item for item in sr["items"] if item["verdict"] == "UNSUPPORTED"]
    flagged_words = {w for item in flagged for w in item["matched_words"]}
    assert flagged_words == {
        "彻底",
        "全部",
        "零",
        "已修",
        "绝不",
        "无任何",
        "100%",
        "never",
        "zero",
        "completely",
    }
    for item in flagged:
        assert item["file"] == "bad_report.md"
        assert item["line_no"] >= 3
        assert item["sentence"]
        assert "bad_report.md" in md_text
        assert str(item["line_no"]) in md_text

    # Supported report: every item containing absolute words ALSO contains a runnable command AND output/exit code
    good_md = tmp_path / "good_report.md"
    good_md.write_text(
        "# M0 自报\n\n"
        "- 全仓零匹配且全部检查已修：运行 `rg 'FAKE_TITLE' src/` -> 退出码 `1`（`0 matches`）。\n"
        "- 彻底跑通单测且 100% 通过：\n"
        "  ```bash\n"
        "  uv run pytest -q\n"
        "  ```\n"
        "  输出：`12 passed in 0.4s`（退出码 `0`）。\n\n"
        "| # | 命令 | 退出码 | 结果 |\n"
        "|---:|---|---:|---|\n"
        "| 1 | `python3 tool.py --check` | `0` | 绝不写用户目录，无任何残留，never writes home, zero leaks, completely clean |\n",
        encoding="utf-8",
    )
    j_good = tmp_path / "good.json"
    rc_good = main(["--lang", "zh", "verify", "--self-report", str(good_md), "--json-out", str(j_good)])
    assert rc_good == 0
    d_good = json.loads(j_good.read_text(encoding="utf-8"))
    assert d_good["conclusion"] == "PASS"
    assert d_good["self_report"]["status"] == "PASS"
    assert all(item["verdict"] == "SUPPORTED" for item in d_good["self_report"]["items"])


def test_denoise_rule1_non_zero_negative_and_positive(tmp_path: Path) -> None:
    # Negative (noise): "非零", "零散", "零点", "non-zero" must NOT be flagged
    noise_md = tmp_path / "rule1_noise.md"
    noise_md.write_text(
        "# 规则 1 反例（不应误报）\n\n"
        "- 非法参数期望返回非零退出码，零散格式与零点边界说明见下文。\n"
        "- Unknown flags return a non-zero exit status.\n",
        encoding="utf-8",
    )
    j_noise = tmp_path / "r1_noise.json"
    rc_noise = main(["--lang", "zh", "verify", "--self-report", str(noise_md), "--json-out", str(j_noise)])
    assert rc_noise == 0
    d_noise = json.loads(j_noise.read_text(encoding="utf-8"))
    assert [i for i in d_noise["self_report"]["items"] if i["verdict"] == "UNSUPPORTED"] == []

    # Positive (true claim without evidence): standalone "零" / "zero" MUST be flagged
    pos_md = tmp_path / "rule1_pos.md"
    pos_md.write_text(
        "# 规则 1 正例（应标无证据）\n\n"
        "- 全程零外部请求，makes zero writes to disk.\n",
        encoding="utf-8",
    )
    j_pos = tmp_path / "r1_pos.json"
    rc_pos = main(["--lang", "zh", "verify", "--self-report", str(pos_md), "--json-out", str(j_pos)])
    assert rc_pos == 1
    d_pos = json.loads(j_pos.read_text(encoding="utf-8"))
    unsup = [i for i in d_pos["self_report"]["items"] if i["verdict"] == "UNSUPPORTED"]
    assert len(unsup) == 1
    assert set(unsup[0]["matched_words"]) == {"零", "zero"}


def test_denoise_rule2_heading_and_table_leadin_negative_and_positive(tmp_path: Path) -> None:
    # Negative (noise): ATX heading with "全部/零" and colon-terminated table lead-in before an evidenced table
    noise_md = tmp_path / "rule2_noise.md"
    noise_md.write_text(
        "### 3. M0 验收表全部 2 行实测结果（零真实调用）\n\n"
        "以下 2 行全部由 `Scripts/acceptance/m0-acceptance.sh` 实际执行：\n\n"
        "| # | 命令 | 退出码 | 输出 |\n"
        "|---:|---|---:|---|\n"
        "| 1 | `pytest tests/test_a.py -q` | `0` | 全部断言通过（`4 passed`） |\n",
        encoding="utf-8",
    )
    j_noise = tmp_path / "r2_noise.json"
    rc_noise = main(["--lang", "zh", "verify", "--self-report", str(noise_md), "--json-out", str(j_noise)])
    assert rc_noise == 0

    # Positive: standalone paragraph claiming "全部...通过。" NOT introducing a table/code block MUST be flagged
    pos_md = tmp_path / "rule2_pos.md"
    pos_md.write_text(
        "### 3. 验收小结\n\n"
        "M0 验收表全部 6 行均已实际执行并通过。\n\n"
        "后续将继续完善文档。\n",
        encoding="utf-8",
    )
    j_pos = tmp_path / "r2_pos.json"
    rc_pos = main(["--lang", "zh", "verify", "--self-report", str(pos_md), "--json-out", str(j_pos)])
    assert rc_pos == 1
    d_pos = json.loads(j_pos.read_text(encoding="utf-8"))
    unsup = [i for i in d_pos["self_report"]["items"] if i["verdict"] == "UNSUPPORTED"]
    assert len(unsup) == 1
    assert unsup[0]["line_no"] == 3
    assert "全部" in unsup[0]["matched_words"]


def test_denoise_rule3_out_of_scope_unverified_section_negative_and_positive(tmp_path: Path) -> None:
    # Negative (noise): Honest disclaimer inside "没验证的项 / 不在范围" section
    noise_md = tmp_path / "rule3_noise.md"
    noise_md.write_text(
        "## 没验证的项和原因\n\n"
        "- 按路线图排在 M1、不在 M0 范围内的项：创建独立 HOME 彻底隔离宿主全局技能，实现零配置冲突。\n",
        encoding="utf-8",
    )
    j_noise = tmp_path / "r3_noise.json"
    rc_noise = main(["--lang", "zh", "verify", "--self-report", str(noise_md), "--json-out", str(j_noise)])
    assert rc_noise == 0

    # Positive: Claiming completion outside out-of-scope section (or boasting "全部通过" with no evidence) MUST be flagged
    pos_md = tmp_path / "rule3_pos.md"
    pos_md.write_text(
        "## 做了什么\n\n"
        "- 创建独立 HOME 彻底隔离宿主全局技能，实现零配置冲突。\n",
        encoding="utf-8",
    )
    j_pos = tmp_path / "r3_pos.json"
    rc_pos = main(["--lang", "zh", "verify", "--self-report", str(pos_md), "--json-out", str(j_pos)])
    assert rc_pos == 1
    d_pos = json.loads(j_pos.read_text(encoding="utf-8"))
    unsup = [i for i in d_pos["self_report"]["items"] if i["verdict"] == "UNSUPPORTED"]
    assert len(unsup) == 1
    assert set(unsup[0]["matched_words"]) == {"彻底", "零"}


def test_denoise_rule4_quoted_deleted_old_copy_negative_and_positive(tmp_path: Path) -> None:
    # Negative (noise): Quoting deleted/retracted old marketing strings
    noise_md = tmp_path / "rule4_noise.md"
    noise_md.write_text(
        "# 删掉的旧文案\n\n"
        '- 删除 README 中的 "零幻觉"、"100% 准确" 与 "Zero raw secrets persisted" 宣传语。\n'
        "- 撤回“绝不读取正文（`never reads prompt text`）”的表述。\n",
        encoding="utf-8",
    )
    j_noise = tmp_path / "r4_noise.json"
    rc_noise = main(["--lang", "zh", "verify", "--self-report", str(noise_md), "--json-out", str(j_noise)])
    assert rc_noise == 0

    # Positive: (a) Quoted marketing string asserted as true (not deleted), and (b) unquoted "全部" outside deleted quote (like K12)
    pos_md = tmp_path / "rule4_pos.md"
    pos_md.write_text(
        "# 新版说明\n\n"
        '- 本次发布实现了 "零幻觉" 与 "100% 准确" 的本地引擎。\n'
        '- 已清除仓库内全部 "Fake Demo Title" 字样。\n',
        encoding="utf-8",
    )
    j_pos = tmp_path / "r4_pos.json"
    rc_pos = main(["--lang", "zh", "verify", "--self-report", str(pos_md), "--json-out", str(j_pos)])
    assert rc_pos == 1
    d_pos = json.loads(j_pos.read_text(encoding="utf-8"))
    unsup = [i for i in d_pos["self_report"]["items"] if i["verdict"] == "UNSUPPORTED"]
    assert len(unsup) == 2
    assert set(unsup[0]["matched_words"]) == {"零", "100%"}
    assert unsup[1]["matched_words"] == ["全部"]
