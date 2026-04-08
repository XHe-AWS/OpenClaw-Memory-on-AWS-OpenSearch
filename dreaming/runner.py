"""
OpenClaw Memory System v2 — Dreaming Runner
=============================================
Orchestrates all three dreaming phases and generates DREAMS.md output.

Usage:
    # As a script (for cron)
    python -m dreaming.runner --agent xiaoxiami

    # From within Python
    from dreaming.runner import DreamingRunner
    runner = DreamingRunner(os_client, embed_client)
    report = runner.run_all(agent_id="xiaoxiami")
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import WORKSPACE_ROOT
from embedding import EmbeddingClient
from opensearch_client import OpenSearchClient
from dreaming.light import LightDreaming
from dreaming.rem import REMDreaming
from dreaming.deep import DeepDreaming

logger = logging.getLogger(__name__)


class DreamingRunner:
    """Orchestrates Light → REM → Deep dreaming phases."""

    def __init__(
        self,
        os_client: OpenSearchClient,
        embed_client: EmbeddingClient,
    ):
        self.os_client = os_client
        self.embed_client = embed_client
        self.light = LightDreaming(os_client, embed_client)
        self.rem = REMDreaming(os_client, embed_client)
        self.deep = DeepDreaming(os_client, embed_client)

    def run_all(self, agent_id: str = "") -> dict[str, Any]:
        """
        Run all three dreaming phases in sequence.

        Returns:
            Combined report from all phases.
        """
        start = time.time()
        full_report = {
            "run_started": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "phases": {},
        }

        # Phase 1: Light
        logger.info("=== Light Phase ===")
        try:
            light_report = self.light.run(agent_id=agent_id)
            full_report["phases"]["light"] = light_report
        except Exception as e:
            logger.error("Light phase failed: %s", e, exc_info=True)
            full_report["phases"]["light"] = {"error": str(e)}

        # Phase 2: REM
        logger.info("=== REM Phase ===")
        try:
            rem_report = self.rem.run(agent_id=agent_id)
            full_report["phases"]["rem"] = rem_report
        except Exception as e:
            logger.error("REM phase failed: %s", e, exc_info=True)
            full_report["phases"]["rem"] = {"error": str(e)}

        # Phase 3: Deep
        logger.info("=== Deep Phase ===")
        try:
            deep_report = self.deep.run(agent_id=agent_id)
            full_report["phases"]["deep"] = deep_report
        except Exception as e:
            logger.error("Deep phase failed: %s", e, exc_info=True)
            full_report["phases"]["deep"] = {"error": str(e)}

        elapsed = time.time() - start
        full_report["run_completed"] = datetime.now(timezone.utc).isoformat()
        full_report["elapsed_seconds"] = round(elapsed, 1)

        # Write DREAMS.md
        self._write_dreams_md(full_report)

        logger.info("All dreaming phases complete in %.1fs", elapsed)
        return full_report

    def _write_dreams_md(self, report: dict) -> None:
        """Generate and write DREAMS.md with the dreaming report."""
        if not WORKSPACE_ROOT:
            logger.warning("WORKSPACE_ROOT not set, skipping DREAMS.md")
            return

        dreams_path = Path(WORKSPACE_ROOT) / "DREAMS.md"
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            "# 🌙 Dreaming Report",
            "",
            f"_Last run: {date_str}_",
            f"_Duration: {report.get('elapsed_seconds', 0)}s_",
            "",
        ]

        # Light Phase
        light = report.get("phases", {}).get("light", {})
        lines.extend([
            "## 💡 Light Phase — Memory Extraction",
            "",
            f"- Messages processed: {light.get('messages_processed', 0)}",
            f"- Candidates extracted: {light.get('candidates_extracted', 0)}",
            f"- Duplicates skipped: {light.get('duplicates_skipped', 0)}",
            f"- Mergeable found: {light.get('mergeable_found', 0)}",
            "",
        ])

        sessions = light.get("sessions", [])
        if sessions:
            lines.append("### Sessions")
            lines.append("")
            for s in sessions[:10]:
                lines.append(
                    f"- `{s.get('session_id', '?')[:20]}...`: "
                    f"{s.get('messages', 0)} msgs → {s.get('stored', 0)} stored, "
                    f"{s.get('skipped_duplicate', 0)} dup"
                )
            lines.append("")

        # REM Phase
        rem = report.get("phases", {}).get("rem", {})
        lines.extend([
            "## 🌀 REM Phase — Theme Analysis",
            "",
            f"- Memories analyzed: {rem.get('memories_processed', 0)}",
            f"- Clusters found: {rem.get('clusters_found', 0)}",
            f"- Consolidation updates: {rem.get('consolidation_updates', 0)}",
            "",
        ])

        themes = rem.get("themes", [])
        if themes:
            lines.append("### Themes")
            lines.append("")
            for t in themes[:5]:
                summary = t.get("theme_summary", "Unknown theme")
                importance = t.get("importance", 0)
                count = t.get("memory_count", 0)
                lines.append(
                    f"- **{summary}** "
                    f"(importance: {importance:.2f}, {count} memories)"
                )
                patterns = t.get("patterns", [])
                for p in patterns[:3]:
                    lines.append(f"  - 📌 {p}")
            lines.append("")

        # Deep Phase
        deep = report.get("phases", {}).get("deep", {})
        lines.extend([
            "## 🧠 Deep Phase — Scoring & Promotion",
            "",
            f"- Candidates scored: {deep.get('candidates_scored', 0)}",
            f"- Promoted to MEMORY.md: {deep.get('promoted', 0)}",
            f"- Below threshold: {deep.get('skipped_threshold', 0)}",
            f"- Too old: {deep.get('skipped_age', 0)}",
            "",
        ])

        dist = deep.get("score_distribution", {})
        if dist:
            lines.append("### Score Distribution")
            lines.append("")
            for bucket, count in sorted(dist.items()):
                bar = "█" * count
                lines.append(f"- `{bucket}`: {bar} ({count})")
            lines.append("")

        promoted = deep.get("promoted_memories", [])
        if promoted:
            lines.append("### Promoted Memories")
            lines.append("")
            for m in promoted:
                cat = m.get("category", "?")
                content = m.get("content", "")
                score = m.get("score", 0)
                lines.append(f"- [{cat}] {content} _(score: {score:.3f})_")
            lines.append("")

        # Footer
        lines.extend([
            "---",
            f"_Generated by OpenClaw Memory System v2 | {date_str}_",
        ])

        try:
            dreams_path.write_text("\n".join(lines), encoding="utf-8")
            logger.info("DREAMS.md written to %s", dreams_path)
        except Exception as e:
            logger.error("Failed to write DREAMS.md: %s", e)


def main():
    """CLI entry point for cron jobs."""
    import argparse

    parser = argparse.ArgumentParser(description="Run dreaming phases")
    parser.add_argument("--agent", default="", help="Agent ID filter")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from config import OPENSEARCH_ENDPOINT
    if not OPENSEARCH_ENDPOINT:
        print("ERROR: OPENSEARCH_ENDPOINT not set")
        return

    os_client = OpenSearchClient()
    embed_client = EmbeddingClient()
    runner = DreamingRunner(os_client, embed_client)
    report = runner.run_all(agent_id=args.agent)

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
