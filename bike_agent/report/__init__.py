"""Report generation: figures, markdown reports, decisions and traceability."""

from bike_agent.report.pipeline import run_reports
from bike_agent.report.text import (
    render_cross_source,
    render_decisions,
    render_forecast_report,
    render_report,
)

__all__ = [
    "render_cross_source",
    "render_decisions",
    "render_forecast_report",
    "render_report",
    "run_reports",
]
