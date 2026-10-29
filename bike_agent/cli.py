"""``bike-agent`` command line interface."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from typing import Any, Sequence

from jsonschema import Draft202012Validator

from bike_agent.config import ConfigError, Layout, load_config, source_config
from bike_agent.data import fetch_source, validate_source
from bike_agent.data.base import DataSourceError
from bike_agent.data.contract import ContractError
from bike_agent.forecast.pipeline import run_baseline_sensitivity, run_forecast
from bike_agent.prep.pipeline import render_prep_report, run_prep
from bike_agent.report.pipeline import run_reports
from bike_agent.risk.pipeline import render_risk_report, run_risk
from bike_agent.sim.pipeline import run_comparison, run_sim_sensitivity, run_simulation
from bike_agent.tools.handlers import ToolError, call_tool
from bike_agent.tools.schemas import TOOL_SCHEMA
from bike_agent.utils import write_json

_DERIVED_DIRS = ("processed", "outputs", "reports", "figures")


def _layout(args: argparse.Namespace) -> Layout:
    return Layout(args.root)


def _config(args: argparse.Namespace) -> dict[str, Any]:
    return load_config(args.config)


def _sources(args: argparse.Namespace, config: dict[str, Any]) -> list[str]:
    if getattr(args, "sources", None):
        return [s.strip() for s in args.sources.split(",") if s.strip()]
    if getattr(args, "source", None):
        return [args.source]
    return list(config.get("sources", {}).keys())


# -- commands ---------------------------------------------------------------
def cmd_data_fetch(args: argparse.Namespace) -> int:
    config, layout = _config(args), _layout(args)
    for source in _sources(args, config):
        result = fetch_source(config, layout, source, force=args.force)
        print(f"[fetch] {source}: {result['rows']:,} contract rows -> {result['contract_path']}")
    return 0


def cmd_data_validate(args: argparse.Namespace) -> int:
    config, layout = _config(args), _layout(args)
    for source in _sources(args, config):
        report = validate_source(config, layout, source)
        print(
            f"[validate] {source}: {report['rows']:,} rows, duplicate {report['duplicate_rate']:.4%}, "
            f"abnormal duration {report['abnormal_duration_rate']:.4%} -> reports/data_quality.{source}.md"
        )
    return 0


def cmd_prep(args: argparse.Namespace) -> int:
    config, layout = _config(args), _layout(args)
    for source in _sources(args, config):
        payload = run_prep(config, layout, source, cell_size=args.cell_size)
        layout.report_path(f"prep.{source}.md").write_text(render_prep_report(source, payload), encoding="utf-8")
        meta = payload["panel"]
        print(
            f"[prep] {source}: {meta['n_cells']} cells x {meta['n_buckets']} buckets, "
            f"zero-departure share {meta['zero_out_share']:.2%}"
        )
    return 0


def cmd_forecast(args: argparse.Namespace) -> int:
    config, layout = _config(args), _layout(args)
    for source in _sources(args, config):
        payload = run_prep(config, layout, source, cell_size=args.cell_size)
        layout.report_path(f"prep.{source}.md").write_text(render_prep_report(source, payload), encoding="utf-8")
        result = run_forecast(config, layout, source, cell_size=args.cell_size, panel=payload["panel_frame"])
        metrics = result["metrics"]
        print(
            f"[forecast] {source}: baseline MAE(out)={metrics['baseline']['out_count']['mae']:.4f} "
            f"model MAE(out)={metrics['model']['out_count']['mae']:.4f}"
        )
        run_baseline_sensitivity(
            config, layout, source,
            list(source_config(config, source).get("sensitivity_cell_sizes_m", [])),
            clean_frame=payload["clean_frame"],
        )
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    config, layout = _config(args), _layout(args)
    for source in _sources(args, config):
        result = run_risk(config, layout, source)
        layout.report_path(f"risk.{source}.md").write_text(render_risk_report(result), encoding="utf-8")
        print(
            f"[risk] {source}: {result['n_replenish']} replenish / {result['n_clear']} clear "
            f"for {result['time_bucket']}"
        )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    config, layout = _config(args), _layout(args)
    source = args.source or config.get("primary_source", "xm")
    result = run_simulation(config, layout, source, args.strategy, budget=args.budget)
    summary = result["summary"]
    print(
        f"[simulate:{args.strategy}] {source}: unmet={summary['unmet_requests']}, "
        f"distance={summary['distance_bike_km']:.2f} bike-km, cost={summary['composite_cost']:.2f}"
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config, layout = _config(args), _layout(args)
    sources = _sources(args, config)
    run_reports(config, layout, sources)
    print(f"[report] wrote reports/, figures/ and root REPORT.md/DECISIONS.md for {', '.join(sources)}")
    return 0


def cmd_repro(args: argparse.Namespace) -> int:
    config, layout = _config(args), _layout(args)
    sources = _sources(args, config)
    layout.ensure()

    if args.fresh:
        for name in _DERIVED_DIRS:
            target = layout.root / ("data/" + name if name == "processed" else name)
            if target.exists():
                shutil.rmtree(target)
        for name in ("REPORT.md", "DECISIONS.md"):
            path = layout.root / name
            if path.exists():
                path.unlink()
        layout.ensure()

    for source in sources:
        fetch_source(config, layout, source, force=False)
        validate_source(config, layout, source)
        prep = run_prep(config, layout, source)
        layout.report_path(f"prep.{source}.md").write_text(
            render_prep_report(source, prep), encoding="utf-8"
        )
        run_baseline_sensitivity(
            config, layout, source,
            list(source_config(config, source).get("sensitivity_cell_sizes_m", [])),
            clean_frame=prep["clean_frame"],
        )
        run_forecast(config, layout, source, panel=prep["panel_frame"])
        risk = run_risk(config, layout, source)
        layout.report_path(f"risk.{source}.md").write_text(render_risk_report(risk), encoding="utf-8")
        run_comparison(config, layout, source)
        run_sim_sensitivity(config, layout, source)

    run_reports(config, layout, sources)
    Draft202012Validator.check_schema(TOOL_SCHEMA)
    write_json(layout.outputs / "tool_schema.json", TOOL_SCHEMA)
    print(f"[repro] complete for {', '.join(sources)}")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    if args.schema:
        Draft202012Validator.check_schema(TOOL_SCHEMA)
        print(json.dumps(TOOL_SCHEMA, indent=2, ensure_ascii=False))
        return 0
    if args.call:
        config, layout = _config(args), _layout(args)
        payload = json.loads(args.input or "{}")
        result = call_tool(args.call, payload, config, layout)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print("usage: bike-agent tools --schema | --call <tool> --input '<json>'", file=sys.stderr)
    return 2


# -- parser ----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bike-agent", description="Shared-bicycle supply/demand analysis")
    parser.add_argument("--root", default=None, help="project root (default: auto-detect)")
    parser.add_argument("--config", default=None, help="YAML config override")
    sub = parser.add_subparsers(dest="command", required=True)

    data = sub.add_parser("data", help="fetch / validate raw data")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    fetch = data_sub.add_parser("fetch")
    fetch.add_argument("--source", choices=["xm", "sh"])
    fetch.add_argument("--force", action="store_true")
    fetch.set_defaults(func=cmd_data_fetch)
    validate = data_sub.add_parser("validate")
    validate.add_argument("--source", choices=["xm", "sh"])
    validate.set_defaults(func=cmd_data_validate)

    prep = sub.add_parser("prep", help="clean and aggregate")
    prep.add_argument("--source", choices=["xm", "sh"])
    prep.add_argument("--cell-size", type=int, default=None)
    prep.set_defaults(func=cmd_prep)

    forecast = sub.add_parser("forecast", help="baseline + model forecast")
    forecast.add_argument("--source", choices=["xm", "sh"])
    forecast.add_argument("--cell-size", type=int, default=None)
    forecast.set_defaults(func=cmd_forecast)

    risk = sub.add_parser("risk", help="risk scoring and priority lists")
    risk.add_argument("--source", choices=["xm", "sh"])
    risk.set_defaults(func=cmd_risk)

    simulate = sub.add_parser("simulate", help="dispatch simulation")
    simulate.add_argument("--strategy", choices=["rule", "optimized"], required=True)
    simulate.add_argument("--source", choices=["xm", "sh"])
    simulate.add_argument("--budget", type=int, default=None)
    simulate.set_defaults(func=cmd_simulate)

    report = sub.add_parser("report", help="render reports and figures")
    report.add_argument("--source", choices=["xm", "sh"])
    report.add_argument("--sources", default=None, help="comma separated list")
    report.set_defaults(func=cmd_report)

    repro = sub.add_parser("repro", help="one-command end-to-end reproduction")
    repro.add_argument("--sources", default=None, help="comma separated list (default: all)")
    repro.add_argument("--fresh", action="store_true", help="delete derived artefacts first")
    repro.set_defaults(func=cmd_repro)

    tools = sub.add_parser("tools", help="agent tool contracts")
    tools.add_argument("--schema", action="store_true")
    tools.add_argument("--call", default=None, help="tool name")
    tools.add_argument("--input", default=None, help="JSON input object")
    tools.set_defaults(func=cmd_tools)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, DataSourceError, ContractError, ToolError, FileNotFoundError, ValueError) as exc:
        print(f"bike-agent: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
