"""F11: an auditable agent layer that only ever sees the F7 tool contracts.

The module is deliberately narrow:

* :mod:`bike_agent.agent.gateway` is the *only* door to data.  It forwards to
  :mod:`bike_agent.tools.handlers` and nothing else -- it has no knowledge of
  where artefacts live on disk, and cannot read one (F11-3).
* :mod:`bike_agent.agent.planner` turns tool returns into a conclusion.  It is
  deterministic: the same artefacts produce the same record.
* :mod:`bike_agent.agent.audit` shapes and serialises the run record.

Nothing here is imported by ``bike-agent repro`` (F11-2), so the pipeline and
its zero-LLM guarantee are untouched when this package is absent (F11-1).
"""

from __future__ import annotations

from bike_agent.agent.audit import RunRecord, render_markdown
from bike_agent.agent.gateway import ToolGateway
from bike_agent.agent.planner import replay_review

__all__ = ["RunRecord", "ToolGateway", "render_markdown", "replay_review"]
