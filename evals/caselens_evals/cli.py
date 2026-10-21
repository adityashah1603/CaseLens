"""`python -m caselens_evals run|score|report|smoke|full`.

* run:    run the agent on gold questions (resumable) + retrieval recall@10 -> a results dir
* score:  judge (cached by run id) + metrics -> scores.json + report.md; exit 1 when citation
          precision < 95% (the hard CI gate)
* report: print report.md; `--readme` writes the README results block; `--compare` adds a model
          comparison table across results dirs
* smoke:  run + score the fixed 10-question subset (also what a bare `python -m caselens_evals`
          and `make eval` do). Skips (exit 0) when the gold set, the database or the model key is
          missing, unless CASELENS_EVAL_REQUIRE_LIVE=1.
* full:   run + score all 50 questions and update the README (`make eval-full`)

Needs CASELENS_DATABASE_URL (dev DB with the ingested demo case) and the model router's
settings (CASELENS_LLM_PROVIDER, CASELENS_GEMINI_API_KEY) for run / smoke / full.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from caselens_evals import report, retrieval_eval, runner
from caselens_evals.gold import GoldItem, load_gold

if TYPE_CHECKING:
    from caselens_agent.router import ModelRouter

GOLD_PATH = Path(__file__).resolve().parents[1] / "gold" / "riverside_lot.jsonl"


def _load(path: Path) -> list[GoldItem]:
    # The set-level mix check applies to the real gold set; fixtures may be partial.
    return load_gold(path, check=path.resolve() == GOLD_PATH.resolve())


def _ids(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def live_skip_reason(gold: Path) -> str | None:
    if not gold.exists():
        return f"no gold set at {gold}"
    if not os.environ.get("CASELENS_DATABASE_URL"):
        return "no ingested case: CASELENS_DATABASE_URL is not set"
    provider = os.environ.get("CASELENS_LLM_PROVIDER", "gemini")
    if provider == "gemini" and not os.environ.get("CASELENS_GEMINI_API_KEY"):
        return "no model key: CASELENS_GEMINI_API_KEY is not set"
    if provider == "fake":
        return "the fake model provider cannot answer gold questions"
    return None


def cmd_run(args: argparse.Namespace) -> Path:
    items = _load(args.gold)
    chosen = runner.select_items(
        items, ids=_ids(args.ids), category=args.category, limit=args.limit, smoke=args.smoke
    )
    if args.out:
        out_dir = Path(args.out)
    elif args.resume:
        latest = runner.latest_results_dir()
        if latest is None:
            raise SystemExit("no results dir to resume")
        out_dir = latest
    else:
        out_dir = runner.new_results_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    overrides = runner.parse_model_overrides(args.model)
    runner.write_meta(
        out_dir,
        {
            "gold": str(args.gold),
            "model_overrides": overrides,
            "smoke": bool(args.smoke),
            "started_at": runner.read_meta(out_dir).get("started_at")
            or datetime.now(UTC).isoformat(),
        },
    )
    url = os.environ.get("CASELENS_DATABASE_URL")
    if not url:
        raise SystemExit("CASELENS_DATABASE_URL is not set")
    print(f"results dir: {out_dir}  ({len(chosen)} questions)")
    with runner.LiveExecutor(
        url, overrides, case_id=args.case_id, user_id=args.user_id
    ) as executor:
        summary = runner.run_items(chosen, out_dir, executor, retry_failed=args.retry_failed)
        print(
            f"ran {len(summary.ran)}, skipped {len(summary.skipped)} already done"
            + (" (stopped early)" if summary.stopped_early else "")
        )
        if not args.skip_retrieval:
            prev = retrieval_eval.load(out_dir)
            todo = [
                i for i in chosen if prev is None or i.id not in {x.gold_id for x in prev.items}
            ]
            if todo:
                rep = retrieval_eval.evaluate(
                    todo,
                    retrieval_eval.live_searcher(executor.app_conn, executor.case_id),
                    retrieval_eval.case_filenames(executor.app_conn, executor.case_id),
                )
                retrieval_eval.save(prev.merged(rep) if prev else rep, out_dir)
    return out_dir


def make_judge_router(judge_model: str | None) -> ModelRouter:
    from caselens_agent.router import ModelRouter

    return ModelRouter(
        runner.router_settings({"model_judge": judge_model} if judge_model else None)
    )


def cmd_score(out_dir: Path, gold: Path, judge: bool, judge_model: str | None) -> int:
    from caselens_evals.judge import judge_runs
    from caselens_evals.metrics import score

    items = {i.id: i for i in _load(gold)}
    runs = runner.load_runs(out_dir)
    if not runs:
        print(f"no runs in {out_dir}", file=sys.stderr)
        return 2
    router = make_judge_router(judge_model) if judge else None
    verdicts, calls = judge_runs(items, runs, out_dir, router)
    retrieval = retrieval_eval.load(out_dir)
    meta = runner.read_meta(out_dir)
    card = score(
        [items[g] for g in sorted(runs) if g in items],
        runs,
        verdicts,
        retrieval=retrieval.recall if retrieval else None,
        date=str(meta.get("started_at", ""))[:10],
        judge_calls=calls,
    )
    report.save(card, out_dir)
    print(report.render_report(card, out_dir.name))
    print(f"judge LLM calls this score: {calls}")
    if not card.gate_passed:
        print("GATE FAILED: citation precision is below 95%", file=sys.stderr)
        return 1
    return 0


def cmd_report(dirs: Sequence[Path], readme: bool) -> int:
    cards = [(d.name, report.load_scorecard(d)) for d in dirs]
    print(report.render_report(cards[0][1], cards[0][0]))
    if len(cards) > 1:
        named = [(f"{report.card_label(c, lbl)} ({lbl})", c) for lbl, c in cards]
        print("## Model comparison\n\n" + report.comparison_table(named))
    if readme:
        report.update_readme(cards)
        print(f"updated {report.README}")
    return 0


def probe_live(args: argparse.Namespace) -> str | None:
    """Why the demo case can't be reached (no DB / not ingested / no seed lead), or None."""
    import psycopg

    try:
        runner.LiveExecutor(
            os.environ["CASELENS_DATABASE_URL"], case_id=args.case_id, user_id=args.user_id
        ).close()
    except (psycopg.Error, RuntimeError) as exc:
        return f"no ingested case: {type(exc).__name__}: {str(exc)[:200]}"
    return None


def _live(args: argparse.Namespace, smoke: bool) -> int:
    reason = live_skip_reason(args.gold)
    if reason is None:
        reason = probe_live(args)
    if reason:
        if os.environ.get("CASELENS_EVAL_REQUIRE_LIVE") == "1":
            print(f"caselens-evals: cannot run live eval: {reason}", file=sys.stderr)
            return 2
        if args.gold.exists():
            n = len(_load(args.gold))
            print(f"caselens-evals: gold set valid ({n} questions).")
        print(f"caselens-evals: live eval skipped ({reason}); passes.")
        return 0
    args.smoke = smoke
    out_dir = cmd_run(args)
    code = cmd_score(out_dir, args.gold, judge=True, judge_model=args.judge_model)
    if not smoke and code != 2:
        report.update_readme([(out_dir.name, report.load_scorecard(out_dir))])
    return code


def _run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gold", type=Path, default=GOLD_PATH)
    p.add_argument("--out", help="results dir (an existing one is resumed)")
    p.add_argument("--resume", action="store_true", help="resume the latest results dir")
    p.add_argument("--limit", type=int)
    p.add_argument("--ids", help="comma-separated gold ids, e.g. q01,q07")
    p.add_argument("--category")
    p.add_argument("--model", help="router overrides: plan=<model>,synthesize=<model>")
    p.add_argument("--retry-failed", action="store_true", help="rerun items whose run failed")
    p.add_argument("--skip-retrieval", action="store_true")
    p.add_argument("--case-id", type=uuid.UUID, help="default: the demo case")
    p.add_argument("--user-id", type=uuid.UUID, help="default: the seed lead")
    p.add_argument("--judge-model")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m caselens_evals")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="run the agent on gold questions")
    _run_options(p_run)
    p_run.add_argument("--smoke", action="store_true", help="the fixed 10-question subset")
    p_score = sub.add_parser("score", help="judge + metrics for a results dir")
    p_score.add_argument("dir", type=Path, nargs="?")
    p_score.add_argument("--gold", type=Path, default=GOLD_PATH)
    p_score.add_argument("--no-judge", action="store_true", help="use cached verdicts only")
    p_score.add_argument("--judge-model")
    p_report = sub.add_parser("report", help="print / publish a scored results dir")
    p_report.add_argument("dirs", type=Path, nargs="*")
    p_report.add_argument("--compare", type=Path, nargs="+", help="results dirs to compare")
    p_report.add_argument("--readme", action="store_true", help="write the README block")
    for name in ("smoke", "full"):
        _run_options(sub.add_parser(name, help=f"run + score ({name})"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        cmd_run(args)
        return 0
    if args.command == "score":
        out_dir = args.dir or runner.latest_results_dir()
        if out_dir is None:
            print("no results dir", file=sys.stderr)
            return 2
        return cmd_score(Path(out_dir), args.gold, not args.no_judge, args.judge_model)
    if args.command == "report":
        dirs = [*args.dirs, *(args.compare or [])]
        if not dirs:
            latest = runner.latest_results_dir()
            if latest is None:
                print("no results dir", file=sys.stderr)
                return 2
            dirs = [latest]
        return cmd_report(dirs, args.readme)
    return _live(args, smoke=args.command == "smoke")
