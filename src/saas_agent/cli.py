"""Minimal command-line entry point for one standalone agent task."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from saas_agent import AgentConfig, AgentTask, run_agent


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    try:
        value = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise SystemExit(f"invalid task file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"task file {path} must contain one object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one reliability-oriented SaaS browser-agent task."
    )
    parser.add_argument("task", type=Path, help="Task YAML or JSON file")
    parser.add_argument(
        "--context",
        type=Path,
        help="Optional tool connection context YAML or JSON file",
    )
    parser.add_argument("--output", type=Path, help="Optional result JSON path")
    parser.add_argument("--model", help="Override SAAS_AGENT_LLM_MODEL")
    parser.add_argument("--max-steps", type=int, help="Override the maximum steps")
    parser.add_argument(
        "--prompt-mode",
        choices=("off", "routing_trimmed", "routing_bucket"),
        help="Override prompt routing mode",
    )
    parser.add_argument(
        "--tool-mode",
        choices=("disabled", "routing"),
        help="Override app capability routing mode",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Run Chromium with a visible window",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    task = AgentTask.from_mapping(_load_mapping(args.task))
    context = _load_mapping(args.context) if args.context else None
    config = AgentConfig.from_env(
        model=args.model,
        max_steps=args.max_steps,
        prompt_mode=args.prompt_mode,
        tool_mode=args.tool_mode,
        headless=False if args.show_browser else None,
    )
    result = asyncio.run(
        run_agent(
            task,
            config,
            tool_context=context,
            output_path=args.output,
        )
    )
    print(
        json.dumps(
            {
                "task_id": result["task_id"],
                "status": result["status"],
                "steps": len(result.get("trajectory") or []),
                "output_path": str(args.output) if args.output else None,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] not in {"error", "infra_interrupted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
