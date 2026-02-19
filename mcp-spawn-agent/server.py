#!/usr/bin/env python3
"""
mcp-spawn-agent v4 — Subscription-based multi-level agent hierarchy.

Runs `claude -p` as subprocesses (subscription) and exposes MCP tools:
  - spawn_agent
  - spawn_batch
  - get_status
  - list_outputs

v4 fixes:
  - Propagate permission/config flags to subprocesses:
      --setting-sources local
      --dangerously-skip-permissions
  - stdin=DEVNULL to avoid hidden prompts hanging without TTY
  - Write *.cmdline.txt for every agent run (debug)
  - spawn_batch runs in waves of MAX_PARALLEL (not silently truncating)
  - Depth enforcement based on SPAWN_DEPTH_REMAINING env var (effective max depth)
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("spawn-agent")

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
PYTHON_BIN = os.environ.get("PYTHON_BIN", "python3")
THIS_SERVER = os.environ.get("THIS_SERVER", os.path.abspath(__file__))

DEFAULT_MODEL = os.environ.get("SPAWN_DEFAULT_MODEL", "opus")
MAX_PARALLEL = int(os.environ.get("SPAWN_MAX_PARALLEL", "3"))   # waves of 3 is a sane default
MAX_DEPTH = int(os.environ.get("SPAWN_MAX_DEPTH", "3"))
AGENT_TIMEOUT = int(os.environ.get("SPAWN_AGENT_TIMEOUT", "900"))  # 15 min default
MAX_OUTPUT_BYTES = int(os.environ.get("SPAWN_MAX_OUTPUT_BYTES", "50000"))

# IMPORTANT: propagate the same CLI flags you used interactively
# You can override via env SPAWN_CHILD_FLAGS_JSON='["--foo","bar"]'
_default_child_flags = ["--setting-sources", "local", "--dangerously-skip-permissions"]
if os.environ.get("SPAWN_CHILD_FLAGS_JSON"):
    try:
        CHILD_CLAUDE_FLAGS = json.loads(os.environ["SPAWN_CHILD_FLAGS_JSON"])
        if not isinstance(CHILD_CLAUDE_FLAGS, list):
            raise ValueError("SPAWN_CHILD_FLAGS_JSON must decode to a JSON list")
    except Exception as e:
        logger.warning(f"Invalid SPAWN_CHILD_FLAGS_JSON; using defaults. Error={e}")
        CHILD_CLAUDE_FLAGS = _default_child_flags
else:
    CHILD_CLAUDE_FLAGS = _default_child_flags

# Effective max depth for THIS server process comes from env
# Top-level (when you start server normally): SPAWN_DEPTH_REMAINING not set => MAX_DEPTH
# Nested servers: parent sets SPAWN_DEPTH_REMAINING for child server.
_env_depth = os.environ.get("SPAWN_DEPTH_REMAINING")
if _env_depth is None:
    EFFECTIVE_MAX_DEPTH = MAX_DEPTH
else:
    try:
        EFFECTIVE_MAX_DEPTH = max(0, min(MAX_DEPTH, int(_env_depth)))
    except ValueError:
        EFFECTIVE_MAX_DEPTH = MAX_DEPTH

# ──────────────────────────────────────────────────────────────────────────────
# Role presets — system prompts
# ──────────────────────────────────────────────────────────────────────────────

ROLES: Dict[str, str] = {
    "default": (
        "You are a specialist system investigator for a Linux machine. "
        "You have sudo access via Bash. Investigate thoroughly. "
        "Always limit command output using head/tail (max ~200 lines per command)."
    ),
    "auditor": (
        "You are a system auditor. Collect RAW FACTS only — command outputs, file contents, runtime state. "
        "NO severity ratings, NO conclusions, NO recommendations. "
        "Track all commands you run. Use sudo. Limit output with head/tail."
    ),
    "verifier": (
        "You are a verification agent. Verify a specific finding against runtime state. "
        "Check /proc, /sys, systemctl status, actual behavior. "
        "If config says X is disabled, confirm it's disabled at runtime. "
        "Report config-vs-runtime discrepancies. "
        "Rate severity ONLY AFTER verification: CRITICAL/HIGH/MEDIUM/LOW/FALSE-POSITIVE. "
        "Provide a specific fix and rollback plan."
    ),
    "log-analyst": (
        "You are a log analyst. Read logs CHRONOLOGICALLY, establish a baseline, build a timeline, "
        "find gaps, correlate journalctl/dmesg/auth logs, detect repeating patterns, and write a narrative. "
        "Facts only; no severity or recommendations. Use sudo. Limit outputs."
    ),
    "reporter": (
        "You are a report generator. Read evidence files and produce a concise, accurate report. "
        "Group by severity. Include evidence pointers. Include false positives and why disproved."
    ),
    "manager": (
        "You are a senior analyst managing sub-agents. Delegate via spawn_agent/spawn_batch. "
        "All data must flow through files on disk. After agents complete, read outputs and summarize."
    ),
}

# ──────────────────────────────────────────────────────────────────────────────
# Global state tracking
# ──────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_stats = {
    "agents_spawned": 0,
    "agents_completed": 0,
    "agents_failed": 0,
    "start_time": 0.0,
}

def _get_stats() -> dict:
    with _lock:
        return dict(_stats)

def _inc_spawned(n: int = 1) -> None:
    with _lock:
        if _stats["start_time"] == 0:
            _stats["start_time"] = time.time()
        _stats["agents_spawned"] += n

def _inc_completed(ok: bool) -> None:
    with _lock:
        if ok:
            _stats["agents_completed"] += 1
        else:
            _stats["agents_failed"] += 1

# ──────────────────────────────────────────────────────────────────────────────
# Depth handling + recursive MCP config
# ──────────────────────────────────────────────────────────────────────────────

def _clamp_depth(requested: int) -> int:
    """Clamp requested depth to this server's effective max depth."""
    requested = int(requested) if isinstance(requested, int) else 0
    if requested < 0:
        requested = 0
    return min(requested, EFFECTIVE_MAX_DEPTH)

def _build_mcp_config_for_child(depth_limit: int) -> str:
    """
    If depth_limit > 0, provide MCP config so the child can spawn.
    Child server gets SPAWN_DEPTH_REMAINING = depth_limit - 1.
    """
    if depth_limit <= 0:
        return ""

    cfg = {
        "mcpServers": {
            "spawn-agent": {
                "command": PYTHON_BIN,
                "args": [THIS_SERVER],
                "env": {
                    "SPAWN_DEPTH_REMAINING": str(depth_limit - 1),
                },
            }
        }
    }
    return json.dumps(cfg)

# ──────────────────────────────────────────────────────────────────────────────
# Core runner: one claude -p subprocess
# ──────────────────────────────────────────────────────────────────────────────

def run_claude_agent(
    agent_name: str,
    prompt: str,
    system_prompt: str = "",
    model: str = DEFAULT_MODEL,
    output_file: str = "",
    depth_limit: int = 0,
    allowed_tools: str = "",
    timeout: int = AGENT_TIMEOUT,
) -> dict:
    """
    Run `claude -p` as a subprocess. Returns dict with:
      result, success, elapsed, exit_code
    """
    depth_limit = _clamp_depth(depth_limit)

    full_prompt = prompt
    if output_file:
        full_prompt += f"\n\nIMPORTANT: Write your complete findings to {output_file}"

    cmd = [CLAUDE_BIN, "-p", full_prompt]
    cmd.extend(CHILD_CLAUDE_FLAGS)

    if model:
        cmd.extend(["--model", model])
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    # Non-interactive tool execution
    cmd.extend(["--permission-mode", "bypassPermissions"])

    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])

    mcp_config = _build_mcp_config_for_child(depth_limit)
    if mcp_config:
        cmd.extend(["--mcp-config", mcp_config])

    cmd.extend(["--output-format", "text", "--no-session-persistence"])

    env = os.environ.copy()
    # Unset nesting detection vars
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    # Debug: write exact command line used
    if output_file:
        try:
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            dbg_path = Path(output_file).with_suffix(".cmdline.txt")
            dbg_path.write_text(" ".join(cmd) + "\n")
        except Exception as e:
            logger.warning(f"Failed to write cmdline debug file: {e}")

    logger.info(f"[agent:{agent_name}] starting (model={model}, depth={depth_limit}, timeout={timeout}s)")
    start = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,   # <-- critical
        )
        elapsed = time.time() - start
        out = result.stdout or ""
        err = result.stderr or ""

        text_out = out if out.strip() else err if err.strip() else "(no output)"
        if len(text_out.encode("utf-8", errors="ignore")) > MAX_OUTPUT_BYTES:
            text_out = text_out[:MAX_OUTPUT_BYTES] + "\n[TRUNCATED]\n"

        ok = (result.returncode == 0)
        _inc_completed(ok)

        if not ok:
            logger.warning(f"[agent:{agent_name}] exit={result.returncode} stderr_head={err[:200]!r}")
        logger.info(f"[agent:{agent_name}] done in {elapsed:.1f}s ok={ok}")

        return {
            "result": text_out,
            "success": ok,
            "elapsed": elapsed,
            "exit_code": result.returncode,
            "depth": depth_limit,
        }

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        _inc_completed(False)
        logger.error(f"[agent:{agent_name}] TIMEOUT after {timeout}s")
        return {
            "result": f"[TIMEOUT: exceeded {timeout}s]",
            "success": False,
            "elapsed": elapsed,
            "exit_code": -1,
            "depth": depth_limit,
        }
    except Exception as e:
        elapsed = time.time() - start
        _inc_completed(False)
        logger.error(f"[agent:{agent_name}] ERROR: {e}")
        return {
            "result": f"[ERROR: {e}]",
            "success": False,
            "elapsed": elapsed,
            "exit_code": -1,
            "depth": depth_limit,
        }

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_system_prompt(arguments: dict) -> str:
    if arguments.get("system_prompt"):
        return arguments["system_prompt"]
    role = arguments.get("role", "default")
    return ROLES.get(role, ROLES["default"])

def _file_info(path: str) -> str:
    try:
        p = Path(path)
        if p.exists():
            st = p.stat()
            return f"{path} ({st.st_size:,} bytes)"
    except Exception:
        pass
    return path

# ──────────────────────────────────────────────────────────────────────────────
# MCP server
# ──────────────────────────────────────────────────────────────────────────────

app = Server("spawn-agent")

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="spawn_agent",
            description=(
                "Spawn one AI sub-agent as a separate `claude -p` subprocess (subscription). "
                "If depth_limit > 0, sub-agent can itself spawn children (recursive)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "prompt": {"type": "string"},
                    "role": {"type": "string", "enum": list(ROLES.keys())},
                    "system_prompt": {"type": "string"},
                    "model": {"type": "string", "enum": ["haiku", "sonnet", "opus"]},
                    "output_file": {"type": "string"},
                    "depth_limit": {"type": "integer"},
                    "allowed_tools": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="spawn_batch",
            description=(
                f"Spawn multiple sub-agents in parallel waves (MAX_PARALLEL={MAX_PARALLEL}). "
                "Returns after all agents complete."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_name": {"type": "string"},
                                "task": {"type": "string"},
                                "output_file": {"type": "string"},
                                "role": {"type": "string", "enum": list(ROLES.keys())},
                                "system_prompt": {"type": "string"},
                                "model": {"type": "string", "enum": ["haiku", "sonnet", "opus"]},
                                "allowed_tools": {"type": "string"},
                                "timeout": {"type": "integer"},
                            },
                            "required": ["agent_name", "task", "output_file"],
                        },
                    },
                    "depth_limit": {"type": "integer"},
                },
                "required": ["agents"],
            },
        ),
        Tool(
            name="get_status",
            description="Get stats about this MCP server session (spawned/completed/failed).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_outputs",
            description="List files in a directory (sizes, mtimes).",
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "pattern": {"type": "string"},
                    "recursive": {"type": "boolean"},
                },
                "required": ["directory"],
            },
        ),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    loop = asyncio.get_event_loop()

    if name == "spawn_agent":
        agent_name = arguments.get("agent_name", "agent")
        prompt = arguments.get("prompt", "")
        role_prompt = _resolve_system_prompt(arguments)
        model = arguments.get("model", DEFAULT_MODEL)
        output_file = arguments.get("output_file", "")
        depth_limit = _clamp_depth(arguments.get("depth_limit", 0))
        allowed_tools = arguments.get("allowed_tools", "")
        timeout = int(arguments.get("timeout", AGENT_TIMEOUT))

        if output_file:
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)

        _inc_spawned(1)
        result = await loop.run_in_executor(
            None,
            lambda: run_claude_agent(
                agent_name=agent_name,
                prompt=prompt,
                system_prompt=role_prompt,
                model=model,
                output_file=output_file,
                depth_limit=depth_limit,
                allowed_tools=allowed_tools,
                timeout=timeout,
            ),
        )

        ok = "OK" if result["success"] else "FAILED"
        preview = result["result"]
        if len(preview) > 3000:
            preview = preview[:1500] + "\n...\n" + preview[-1200:]

        parts = [
            f"Agent '{agent_name}' finished: {ok}",
            f"Elapsed: {result['elapsed']:.1f}s depth={result['depth']} model={model}",
        ]
        if output_file:
            parts.append(f"Output file: {_file_info(output_file)}")
            parts.append(f"Cmdline file: {_file_info(str(Path(output_file).with_suffix('.cmdline.txt')))}")
        parts.append("---")
        parts.append(preview)

        s = _get_stats()
        parts.append("---")
        parts.append(
            f"Session: {s['agents_completed']}/{s['agents_spawned']} completed, "
            f"{s['agents_failed']} failed. Depth policy: EFFECTIVE_MAX_DEPTH={EFFECTIVE_MAX_DEPTH}"
        )

        return [TextContent(type="text", text="\n".join(parts))]

    if name == "spawn_batch":
        agents_spec = arguments.get("agents", [])
        if not agents_spec:
            return [TextContent(type="text", text="[ERROR: empty agents list]")]

        depth_limit = _clamp_depth(arguments.get("depth_limit", 0))

        # Ensure output dirs
        for spec in agents_spec:
            outf = spec.get("output_file", "")
            if outf:
                Path(outf).parent.mkdir(parents=True, exist_ok=True)

        results: List[Tuple[str, str, dict]] = []
        start = time.time()

        # Run in waves
        for i in range(0, len(agents_spec), MAX_PARALLEL):
            wave = agents_spec[i : i + MAX_PARALLEL]
            _inc_spawned(len(wave))

            def _run_one(spec: dict) -> Tuple[str, str, dict]:
                return (
                    spec.get("agent_name", "unnamed"),
                    spec.get("output_file", ""),
                    run_claude_agent(
                        agent_name=spec.get("agent_name", "unnamed"),
                        prompt=spec.get("task", ""),
                        system_prompt=_resolve_system_prompt(spec),
                        model=spec.get("model", DEFAULT_MODEL),
                        output_file=spec.get("output_file", ""),
                        depth_limit=depth_limit,
                        allowed_tools=spec.get("allowed_tools", ""),
                        timeout=int(spec.get("timeout", AGENT_TIMEOUT)),
                    ),
                )

            wave_results = await loop.run_in_executor(None, lambda: _run_batch(wave, _run_one))
            results.extend(wave_results)

        elapsed = time.time() - start

        lines = [f"Batch complete: {len(results)} agents. elapsed={elapsed:.1f}s"]
        for aname, outf, r in results:
            status = "OK" if r["success"] else "FAIL"
            file_info = f" -> {_file_info(outf)}" if outf else ""
            lines.append(f"  [{status}] {aname}: {r['elapsed']:.1f}s{file_info}")

        s = _get_stats()
        lines.append(
            f"\nSession: {s['agents_completed']}/{s['agents_spawned']} completed, "
            f"{s['agents_failed']} failed. Depth policy: EFFECTIVE_MAX_DEPTH={EFFECTIVE_MAX_DEPTH}"
        )

        return [TextContent(type="text", text="\n".join(lines))]

    if name == "get_status":
        s = _get_stats()
        elapsed = time.time() - s["start_time"] if s["start_time"] else 0
        return [TextContent(
            type="text",
            text=(
                f"Elapsed: {elapsed:.0f}s ({elapsed/60:.1f} min)\n"
                f"Agents: {s['agents_spawned']} spawned, {s['agents_completed']} completed, {s['agents_failed']} failed\n"
                f"Depth policy: EFFECTIVE_MAX_DEPTH={EFFECTIVE_MAX_DEPTH}"
            ),
        )]

    if name == "list_outputs":
        directory = arguments.get("directory", "/tmp/audit")
        pattern = arguments.get("pattern", "*.md")
        recursive = bool(arguments.get("recursive", True))

        p = Path(directory)
        if not p.is_dir():
            return [TextContent(type="text", text=f"[ERROR: {directory} not found]")]

        entries = sorted(p.rglob(pattern) if recursive else p.glob(pattern))
        if not entries:
            return [TextContent(type="text", text=f"No {pattern} files in {directory}")]

        total_size = 0
        lines = [f"Files in {directory} ({pattern}):"]
        for e in entries:
            try:
                st = e.stat()
                total_size += st.st_size
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                rel = e.relative_to(p)
                lines.append(f"  {mtime}  {st.st_size:>10,}  {rel}")
            except Exception:
                lines.append(f"  ?  {e}")

        lines.append(f"\nTotal: {len(entries)} files, {total_size:,} bytes")
        return [TextContent(type="text", text="\n".join(lines))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]

def _run_batch(specs: List[dict], run_fn):
    """Run a wave in parallel threads."""
    results = []
    with ThreadPoolExecutor(max_workers=min(len(specs), MAX_PARALLEL)) as pool:
        futures = {pool.submit(run_fn, spec): spec for spec in specs}
        for future in as_completed(futures):
            spec = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                results.append((
                    spec.get("agent_name", "?"),
                    spec.get("output_file", ""),
                    {"result": f"[FAILED: {e}]", "success": False, "elapsed": 0, "exit_code": -1, "depth": 0},
                ))
    return results

# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    logger.info(f"spawn-agent v4 starting... EFFECTIVE_MAX_DEPTH={EFFECTIVE_MAX_DEPTH} MAX_PARALLEL={MAX_PARALLEL}")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
