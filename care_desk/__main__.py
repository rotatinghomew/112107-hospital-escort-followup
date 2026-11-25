"""Command line entry point: ``python3 -m care_desk report``."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .project import CareProject
from .reporting import render_comparison_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="陪诊整段确认、历史重放与加班报价比较")
    parser.add_argument("command", nargs="?", default="report", choices=["report", "compare", "history"])
    parser.add_argument("--data", default="data", help="CSV 数据目录")
    parser.add_argument("--out", default="reports", help="报告输出目录")
    parser.add_argument(
        "--decision-as-of",
        default=None,
        help="报价受理判断时点，例如 2026-06-15T09:00；不给定则不检查 valid_until",
    )
    parser.add_argument("--save-run", action="store_true", help="同时保存带源文件哈希的运行快照")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "history":
        project = CareProject(args.data)
        print(json.dumps(project.list_run_snapshots(Path(args.out) / "history"), ensure_ascii=False, indent=2))
        return 0

    project = CareProject(args.data)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    comparison = project.compare_quotes(decision_at=args.decision_as_of)
    if args.decision_as_of is None:
        now_cst = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None, microsecond=0)
        current = project.compare_quotes(decision_at=now_cst)
        comparison["current_acceptance_at"] = now_cst.isoformat(timespec="minutes")
        comparison["current_recommendation"] = current["recommendation"]
    coordination = project.confirmation_plan()
    archive_outcomes = project.actual_outcomes("2026-06-11T12:00")
    current_outcomes = project.actual_outcomes()

    project.save_json(comparison, "quote-comparison.json", out)
    project.save_json(coordination, "coordination-plan.json", out)
    project.save_json(current_outcomes, "outcomes-current.json", out)
    project.save_json(archive_outcomes, "outcomes-archive-20260611.json", out)

    markdown = render_comparison_markdown(comparison, coordination, archive_outcomes)
    (out / "report.md").write_text(markdown, encoding="utf-8")

    if args.save_run:
        snapshot_path = project.save_run_snapshot(
            comparison, label="quote-comparison", out_dir=out / "history"
        )
        print(f"run snapshot: {snapshot_path}")

    print(f"reports written to {out}")
    print(comparison["recommendation"]["code"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
