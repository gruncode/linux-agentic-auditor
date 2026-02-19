# Claude Audit Pipeline

A multi-agent system audit pipeline that orchestrates 50+ AI agents to discover, evaluate,
and verify security findings on Linux systems. Built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
with a custom MCP server for agent spawning.

## How It Works

The pipeline runs in 5 phases:

1. **Discovery** — 16 worker agents (8 domains x 2 models) independently collect system facts
2. **Evaluation** — 8 evaluator agents analyze each domain's findings from both models
3. **Merge** — The manager deduplicates, assigns finding IDs, and classifies for verification
4. **Verification** — Opus verifier agents confirm findings against runtime state and assign severity
5. **Report** — A final report groups findings by severity with fixes and evidence

The dual-model approach (Sonnet + Opus) catches ~15% more findings than either model alone.
An evaluator layer prevents the context-window problem where merging 800+ facts loses
lower-priority observations.

See [docs/architecture.md](docs/architecture.md) for detailed diagrams and design decisions.

## Prerequisites

- **Claude Code** — Anthropic's CLI tool ([install guide](https://docs.anthropic.com/en/docs/claude-code))
- **Claude Pro or Max subscription** — Required for `claude -p` subprocess spawning
- **Python 3.11+**
- **pip** — For installing the MCP dependency
- **sudo access** on the target machine (the audit agents need to read system state)

## Quick Setup

### 1. Install the MCP server dependency

```bash
cd mcp-spawn-agent
pip install -r requirements.txt
```

### 2. Configure Claude Code to use the MCP server

Copy `examples/mcp-config.json` to your Claude Code MCP config and update the path:

```bash
# Edit ~/.claude/mcp.json (or merge into your existing config)
{
  "mcpServers": {
    "mcp-spawn-agent": {
      "command": "python3",
      "args": ["/absolute/path/to/claude-audit-pipeline/mcp-spawn-agent/server.py"]
    }
  }
}
```

### 3. Run the audit

Start Claude Code and paste the contents of `prompts/ensemble-auditor-v3.md`:

```bash
claude
# Then paste the prompt content, or:
cat prompts/ensemble-auditor-v3.md | claude -p --dangerously-skip-permissions
```

Output goes to `~/audits/YYYY-MM-DD/`.

## Configuration

All configuration is via environment variables (set in `mcp.json` or your shell):

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_BIN` | `claude` | Path to the Claude CLI binary |
| `PYTHON_BIN` | `python3` | Python interpreter for child MCP servers |
| `SPAWN_DEFAULT_MODEL` | `opus` | Default model for spawned agents |
| `SPAWN_MAX_PARALLEL` | `3` | Max agents running simultaneously per wave |
| `SPAWN_MAX_DEPTH` | `3` | Max recursive spawning depth |
| `SPAWN_AGENT_TIMEOUT` | `900` | Default timeout per agent (seconds) |
| `SPAWN_MAX_OUTPUT_BYTES` | `50000` | Max stdout captured per agent |
| `SPAWN_CHILD_FLAGS_JSON` | `["--setting-sources","local","--dangerously-skip-permissions"]` | CLI flags passed to child agents |

## Repository Structure

```
claude-audit-pipeline/
├── README.md                          # This file
├── mcp-spawn-agent/
│   ├── server.py                      # MCP server — spawns claude -p subprocesses
│   └── requirements.txt              # Python deps
├── prompts/
│   └── ensemble-auditor-v3.md         # Master orchestration prompt
├── examples/
│   ├── mcp-config.json               # Template MCP config for Claude Code
│   └── sample-output/                 # Synthetic example output
│       ├── state.json
│       ├── phase1/
│       │   └── 01-processes-sonnet/
│       │       └── index.json
│       └── phase2/
│           ├── findings.json
│           └── FND-001/
│               └── verify.md
└── docs/
    └── architecture.md                # Mermaid diagrams + pipeline explanation
```

## Customization

### Adding audit domains

Edit the prompt in `prompts/ensemble-auditor-v3.md`. Add a new domain number (e.g., `09 Containers`)
to the domain list and update the wave schedule.

### Changing models

The prompt specifies `model=sonnet` and `model=opus` for workers. You can change these
to any model supported by your Claude subscription. The evaluators and verifiers default
to Opus for analytical quality.

### Custom role presets

Edit the `ROLES` dict in `server.py` to add domain-specific system prompts. Reference them
with `role="your-role-name"` in spawn calls.

### Adjusting parallelism

Set `SPAWN_MAX_PARALLEL` higher if your subscription supports more concurrent sessions.
The default of 3 is conservative and works with Pro subscriptions.

## Example Output

The `examples/sample-output/` directory contains synthetic (fictional) example data showing
the structure of a completed audit. No real system data is included.

## How the MCP Server Works

The spawn-agent server is a Python [MCP](https://modelcontextprotocol.io/) server that:

1. Receives tool calls from Claude Code via stdio JSON-RPC
2. Translates `spawn_agent` / `spawn_batch` calls into `claude -p` subprocess invocations
3. Manages parallel execution in configurable wave sizes
4. Supports recursive depth (agents can spawn sub-agents up to `MAX_DEPTH` levels)
5. Tracks session statistics (spawned/completed/failed counts)
6. Writes debug `.cmdline.txt` files for every agent run

See [docs/architecture.md](docs/architecture.md) for the full architecture diagram.

