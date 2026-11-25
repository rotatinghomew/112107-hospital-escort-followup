"""Human-readable Markdown rendering for coordination and quote comparisons."""

from __future__ import annotations

from typing import Any


def render_comparison_markdown(report: dict[str, Any], coordination: dict[str, Any], archive_outcomes: dict[str, Any]) -> str:
    lines: list[str] = []
    rec = report["recommendation"]
    lines.append("# 陪诊整段确认与加班报价重放报告")
    lines.append("")
    lines.append(f"## 结论：{rec['code']}")
    lines.append("")
    lines.append(rec["text"])
    if rec.get("recommended_plans"):
        lines.append("")
        lines.append("历史重放推荐报价：**" + "、".join(rec["recommended_plans"]) + "**。")
    current = report.get("current_recommendation")
    if current:
        lines.append("")
        lines.append(
            f"报价受理时点 {report.get('current_acceptance_at')}：{current['code']} — {current['text']}"
        )
    lines.append("")

    lines.append("## 可比重放工作日")
    lines.append("")
    lines.append("| 日期 | 星期 | 单元 | 受理截点 | 完整性 |")
    lines.append("|---|---:|---|---|---|")
    for day in report["comparable_complete_days"]:
        lines.append(
            f"| {day['day']} | {day['weekday']} | {day['unit']} | {day['cutoff']} | complete={day['complete']} |"
        )
    lines.append("")
    lines.append("排除：" + "；".join(f"{x['day']}（{x['reason']}）" for x in report["excluded_days"]))
    lines.append("")

    lines.append("## 报价比较")
    lines.append("")
    lines.append(
        "| 报价 | 时段/班数 | 重放日 | 净增户次日 | 不同家庭 | 类别 | 每日费用(分) | 重放费用(分) | 每增量户成本(分) |"
    )
    lines.append("|---|---|---:|---:|---:|---|---:|---:|---:|")
    for item in report["comparisons"]:
        categories = "、".join(
            f"{item['added_category_labels'].get(k, k)}×{v}"
            for k, v in item["added_category_counts"].items()
        ) or "无"
        lines.append(
            "| {plan} | {window}/{shift_count} | {days} | {added} | {homes} | {cats} | {cost_day} | {cost_replay} | {cost_each} |".format(
                plan=item["plan"],
                window=item["window"],
                shift_count=item["shift_count_per_day"],
                days=",".join(item["replayed_days"]),
                added=item["additional_household_day_trips"],
                homes=",".join(item["distinct_added_homes"]) or "-",
                cats=categories,
                cost_day=item["cost_cents_per_applicable_day"],
                cost_replay=item["replay_cost_cents_for_all_comparable_days"],
                cost_each=item["cost_cents_per_added_household_in_replay"] or "-",
            )
        )
    lines.append("")
    lines.append("费用单位 `cost_cents` 为分；报价说明定义为每个适用服务日总价。重放费用按可比工作日数累计，另保留每日价。")
    lines.append("")

    lines.append("## 每日净新增与未接原因")
    lines.append("")
    for item in report["comparisons"]:
        lines.append(f"### {item['plan']}")
        if not item["replay_day_details"]:
            lines.append("- 没有适用的完整工作日。")
            continue
        for replay in item["replay_day_details"]:
            lines.append(
                f"- **{replay['day']}**：新增 {replay['additional_count']} 户；"
                f"baseline={','.join(replay['baseline_served_keys'])}，"
                f"quote={','.join(replay['quote_served_keys'])}。"
            )
            for added in replay["additional_households"]:
                lines.append(
                    f"  - 新增 {added['demand_key']} / {added['booking']}：{added['trip_kind_label']}"
                )
            for remaining in replay["still_unaccepted"]:
                reason_text = "；".join(
                    reason.get("message", reason.get("code", ""))
                    for reason in remaining["reasons"][:3]
                )
                lines.append(f"  - 仍未接 {remaining['demand_key']}：{reason_text}")
        substitutions = item.get("substitution_audit", [])
        if substitutions:
            lines.append(
                "  - 挪班审计：该报价在技术时段上可覆盖 "
                + "、".join(
                    f"{x['day']} {x['trip']}/{x['role']}（{x['old_shift']}→{','.join(x['quote_shifts_able_to_replace'])}）"
                    for x in substitutions
                )
                + "；这些替换均不计为新增户。"
            )
    lines.append("")

    lines.append("## 2026-06-10 当前项目的整段确认（as-of 2026-06-08T13:05）")
    lines.append("")
    lines.append("优先级键为 `(预约 expires, household.requested_at, home)`；无法形成原子整段安排时不占用 partial crew。")
    lines.append("")
    lines.append("| 家庭 | 状态 | 选中预约 | 行程类别 | 已有行程 | 说明 |")
    lines.append("|---|---|---|---|---|---|")
    for row in coordination["results"]:
        note = ""
        if row["relay"]:
            note = f"授权接棒 {row['relay']['authorization']} @ {row['relay']['location']}"
        elif row["status"] == "unable_complete":
            note = "；".join(r.get("message", r.get("code", "")) for r in row["reasons"][:2])
        elif row["status"] == "already_confirmed":
            note = "已确认承诺保持不变"
        lines.append(
            f"| {row['home']} | {row['status']} | {row.get('selected_booking') or '-'} | "
            f"{row.get('trip_kind') or '-'} | {row.get('already_confirmed_trip') or '-'} | {note} |"
        )
    lines.append("")

    lines.append("## 实际履行分类（全量回执视图）")
    lines.append("")
    lines.append("```json")
    lines.append(_json_dumps(report["actual_outcomes_summary"]["checkpoint_counts"]))
    lines.append("```")
    lines.append("")
    lines.append("### 归档时点 2026-06-11T12:00 的待核状态")
    lines.append("")
    a4_archive = next(
        (b for b in archive_outcomes["bookings"] if b["booking"] == "A4"), None
    )
    if a4_archive:
        for checkpoint in a4_archive["checkpoints"]:
            lines.append(
                f"- {checkpoint['point_type']} / {checkpoint['point']}：{checkpoint['status']}"
            )
        lines.append("- R32 在 2026-06-12T12:00 补报有效家属签收后可补完；实际时间 2026-06-10T10:40 与登记时间均保留。")
    lines.append("")
    lines.append("## 审计与重跑")
    lines.append("")
    lines.append("- 历史重放不回写 `data/` 中任何原行程或真实办理记录。")
    lines.append("- JSON 报告含每个重放日的 cutoff、纳入预约、原班组、受保护承诺和源 CSV SHA-256。")
    lines.append("- 新工作周可替换/扩充数据目录后运行 `python3 -m care_desk report --data <目录> --save-run`。")
    lines.append("")
    return "\n".join(lines)


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2)
