"""Phase 6: an auditable, rule-driven multi-agent dispatch decision loop.

The package is deliberately separate from :mod:`bike_agent.agent` (F11).  The
old single-agent layer and its three tool contracts stay exactly as they were;
this package adds a *versioned* evaluation contract and a four-role protocol on
top of the same simulation engine.

``bike-agent repro`` never runs this package (NF-34): it builds the replay site,
and the site's collaboration page imports only :mod:`multi_agent.contracts` for
the role and message-type labels.  No agent, gateway, model SDK or credential is
loaded by ``repro``; the pipeline's zero-LLM guarantee is unchanged.

Module map:

* :mod:`bike_agent.multi_agent.contracts` -- task, message and tool schemas.
* :mod:`bike_agent.multi_agent.task` -- task construction, budget options,
  input freezing.
* :mod:`bike_agent.multi_agent.messages` -- the validated message store.
* :mod:`bike_agent.multi_agent.gateway` -- the only door to business data,
  with role permissions, caching and an audit trail.
* :mod:`bike_agent.multi_agent.roles` -- Coordinator, Diagnosis, Dispatch and
  Evaluation, each with its own ``handle`` interface.
* :mod:`bike_agent.multi_agent.orchestrator` -- the bounded loop and terminal
  states.
* :mod:`bike_agent.multi_agent.audit` -- the per-run directory writer.
"""

from __future__ import annotations

__all__ = ["audit", "contracts", "gateway", "messages", "orchestrator", "roles", "task"]
