"""ConsolidationAgent — runs the Librarian memory consolidation pass."""

from __future__ import annotations

import logging
from typing import Callable

from yoloscribe_io import AgentDefinition, SignalEntry, SignalLog, MemoryFile, conclusion_to_dict
from yoloscribe_io.librarian import _conclusion_from_dict

from .base import BaseAgent
from .search import SearchBackend
from ..memory_reasoner import ConsolidationMemoryReasoner

log = logging.getLogger(__name__)


class ConsolidationAgent(BaseAgent):
    """Platform-provisioned agent that runs the Librarian memory consolidation pass.

    Scheduled nightly (default: 0 2 * * *). Does not use an LLM agent loop —
    it calls ConsolidationMemoryReasoner directly, applies decay, runs population
    lint, and writes results without interactive tool calls.
    """

    def _build_system_prompt(self) -> str:
        return ""

    def run(self, prompt: str) -> int:  # noqa: ARG002
        import yaml

        site = self._site
        storage = self._storage

        # 1. Read the full signal log.
        sl = SignalLog(site=site, storage=storage)
        signal_log_text = sl.read_all()

        # 2. Read existing conclusions.
        mf = MemoryFile(site=site, storage=storage)
        _, existing = mf.read()
        existing_yaml = (
            yaml.dump(
                [conclusion_to_dict(c) for c in existing],
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            if existing
            else ""
        )

        # 3. Run consolidation reasoner.
        new_dicts, decay_ids = ConsolidationMemoryReasoner().consolidate(
            signal_log_text, existing_yaml
        )

        # 4. Upsert new inductive/abductive conclusions.
        if new_dicts:
            new_conclusions = [
                _conclusion_from_dict(d) for d in new_dicts if isinstance(d, dict)
            ]
            created, updated, rejected = mf.upsert(new_conclusions)
            log.info(
                "ConsolidationAgent: upserted conclusions — created=%d updated=%d rejected=%d",
                created, updated, len(rejected),
            )
            if rejected:
                log.warning("ConsolidationAgent: rejected conclusions: %s", rejected)

        # 5. Apply decay to stale conclusions.
        if decay_ids:
            fm, conclusions = mf.read()
            by_id = {c.id: c for c in conclusions}
            decayed = 0
            for cid in decay_ids:
                if cid in by_id and by_id[cid].status == "active":
                    by_id[cid].status = "decaying"
                    decayed += 1
            if decayed:
                mf.write(fm, list(by_id.values()))
                log.info("ConsolidationAgent: marked %d conclusions as decaying", decayed)

        # 6. Population lint pass.
        _run_population_lint(site, storage, signal_log_text, self._notify_fn)

        return 0


def _run_population_lint(
    site: str,
    storage,
    signal_log_text: str,
    notify_fn: Callable[[str, dict, str], None],
) -> None:
    """Scan all agent.md files and report agents that have never run."""
    try:
        # Find all agent.md keys under this site.
        prefix = f"{site}/"
        all_keys = storage.list(prefix)
        agent_keys = [k for k in all_keys if k.endswith("/agent.md") and "/.agents/" in k]

        if not agent_keys:
            return

        # Identify agents that have run by scanning signal log for agent_run_success/failure.
        run_keys: set[str] = set()
        for line in signal_log_text.splitlines():
            if "agent_md_key:" in line:
                parts = line.split("agent_md_key:", 1)
                if len(parts) == 2:
                    run_keys.add(parts[1].strip())

        never_run = [k for k in agent_keys if k not in run_keys]

        if not never_run:
            log.info("ConsolidationAgent: population lint — all %d agents have run", len(agent_keys))
            return

        # Write lint report to .user/librarian/lint-report.md.
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            f"# Population Lint Report\n",
            f"Generated: {now}\n\n",
            f"Agents scanned: {len(agent_keys)}\n",
            f"Never run: {len(never_run)}\n\n",
            "## Agents That Have Never Run\n\n",
        ]
        for k in sorted(never_run):
            lines.append(f"- `{k}`\n")

        report_key = f"{site}/.user/librarian/lint-report.md"
        storage.write(report_key, "".join(lines))
        log.info(
            "ConsolidationAgent: lint report — %d/%d agents never run",
            len(never_run), len(agent_keys),
        )

        notify_fn("population_lint", {
            "agents_scanned": len(agent_keys),
            "never_run": len(never_run),
            "report_key": report_key,
        }, "")

    except Exception as exc:
        log.warning("ConsolidationAgent: population lint failed: %s", exc)
