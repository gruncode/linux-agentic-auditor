# Architecture

## MCP Spawn-Agent Server

The spawn-agent MCP server is the execution engine. It exposes four tools to Claude Code
and manages `claude -p` subprocesses with depth-limited recursion.

```mermaid
flowchart TD
    subgraph CLIENT["MCP Client (Claude Code)"]
        CC[claude interactive session]
    end

    CC <-->|stdio JSON-RPC| SERVER

    subgraph SERVER["MCP Server (spawn-agent v4)"]
        direction TB
        APP["Server('spawn-agent')<br/>async main -> stdio_server"]

        subgraph TOOLS["Exposed Tools"]
            SA[spawn_agent<br/>single agent]
            SB[spawn_batch<br/>parallel waves]
            GS[get_status<br/>session stats]
            LO[list_outputs<br/>directory listing]
        end

        subgraph CONFIG["Configuration (env vars)"]
            CONF["CLAUDE_BIN | PYTHON_BIN | THIS_SERVER<br/>DEFAULT_MODEL | MAX_PARALLEL=3<br/>MAX_DEPTH=3 | AGENT_TIMEOUT=900s<br/>MAX_OUTPUT_BYTES=50K<br/>CHILD_CLAUDE_FLAGS"]
        end

        subgraph ROLES["Role Presets -> system_prompt"]
            R1[default]
            R2[auditor]
            R3[verifier]
            R4[log-analyst]
            R5[reporter]
            R6[manager]
        end

        subgraph STATE["Thread-safe Stats"]
            STATS["agents_spawned<br/>agents_completed<br/>agents_failed<br/>start_time"]
        end

        APP --> TOOLS
    end

    SA -->|"run_in_executor"| CORE
    SB -->|"ThreadPoolExecutor<br/>waves of MAX_PARALLEL"| CORE

    ROLES -.->|"_resolve_system_prompt()"| CORE
    CONFIG -.-> CORE
    STATE -.->|"_inc_spawned / _inc_completed"| CORE

    subgraph CORE["Core Runner: run_claude_agent()"]
        direction TB
        BUILD["Build command:<br/>claude -p PROMPT<br/>--model MODEL<br/>--system-prompt ROLE<br/>--permission-mode bypassPermissions<br/>--output-format text<br/>--no-session-persistence"]
        DEPTH{"depth_limit > 0?"}
        MCP_CFG["Inject --mcp-config<br/>child spawn-agent server<br/>SPAWN_DEPTH_REMAINING = depth - 1"]
        NO_MCP["No MCP config<br/>leaf worker"]
        EXEC["subprocess.run()<br/>stdin=DEVNULL<br/>capture_output=True<br/>timeout enforcement"]

        BUILD --> DEPTH
        DEPTH -->|Yes| MCP_CFG --> EXEC
        DEPTH -->|No| NO_MCP --> EXEC
    end

    EXEC -->|stdout/stderr| OUTPUT

    subgraph OUTPUT["Output Handling"]
        TRUNC["Truncate to MAX_OUTPUT_BYTES"]
        DISK["Write to output_file<br/>(on disk)"]
        DBG["Write .cmdline.txt<br/>(debug)"]
        TRUNC --> DISK
        TRUNC --> DBG
    end

    EXEC -->|"depth > 0: child can recurse"| CHILD

    subgraph CHILD["Recursive Child Agent"]
        CHILD_CLAUDE["claude -p subprocess<br/>with own MCP spawn-agent<br/>SPAWN_DEPTH_REMAINING = N-1"]
        CHILD_CLAUDE -->|"can spawn_agent /<br/>spawn_batch"| GRANDCHILD["Deeper agents...<br/>(until depth=0)"]
    end

    GS --> STATE
    LO -->|"Path.rglob()"| DISK_READ["Read directory<br/>sizes + mtimes"]
```

## Audit Pipeline Flow

The ensemble auditor prompt orchestrates 50+ agents across 5 phases:

```mermaid
flowchart LR
    subgraph P1["Phase 1: Discovery"]
        direction TB
        W1["8 domains x 2 models<br/>= 16 workers"]
        W1 --> E1["8 evaluators<br/>(1 per domain)"]
    end

    subgraph P2["Phase 2: Merge"]
        direction TB
        M1["Manager merges<br/>8 candidate lists"]
        M1 --> M2["Deduplicate +<br/>assign FND-IDs"]
        M2 --> M3["Classify:<br/>MUST-VERIFY vs PARK"]
        M3 --> M4["Regression check<br/>vs previous audit"]
    end

    subgraph P3["Phase 3: Verify"]
        direction TB
        V1["STRONG verifiers<br/>(1 per finding)"]
        V1 --> V2["WEAK triage<br/>(batched by domain)"]
    end

    subgraph P4["Phase 4: Report"]
        R1["Final report<br/>grouped by severity"]
    end

    P1 --> P2 --> P3 --> P4
```

## Phase Details

### Phase 1 — Dual-Model Discovery

Each of the 8 audit domains gets **two independent workers** — one running Sonnet, one running Opus.
This redundancy is intentional: different models notice different things.

Workers collect raw facts (command outputs, file contents) and flag anomalies as candidates
with STRONG or WEAK signals. They do NOT assign severity — that's the verifier's job.

**Domains:**

| # | Domain | What it checks |
|---|--------|----------------|
| 01 | Processes/Resources | Running processes, memory, OOM config, swap |
| 02 | Systemd | Failed units, boot performance, cron jobs |
| 03 | Network | Listening ports, firewall, DNS, routing |
| 04 | Storage | LUKS, SMART, filesystem health, mount config |
| 05 | System Config | GRUB, sysctl, kernel modules, PAM, sudoers |
| 06 | Software | Packages, repos, security updates, containers |
| 07 | Hardware/Users | Sensors, accounts, SSH keys, permissions |
| 08 | Logs | Journal, dmesg, auth logs, rotation status |

### Phase 1.5 — Domain Evaluators

After all 16 workers complete, **8 evaluator agents** (one per domain) analyze both workers'
outputs with a clean context window. They:

- Compare Sonnet and Opus findings for agreement/disagreement
- Look for cross-fact patterns (e.g., "OOM killed X" + "X was a screen locker" = security breach)
- Produce a ranked, deduplicated candidate list per domain

### Phase 2 — Manager Merge

The manager (the orchestrating Claude session) reads all 8 evaluator outputs and:

1. Deduplicates across domains
2. Assigns finding IDs (FND-001, FND-002, ...)
3. Classifies: MUST-VERIFY (STRONG, or WEAK from both models) vs PARK (WEAK from one model)
4. Runs regression check against the previous audit (if any)

### Phase 3 — Verification

Opus verifier agents check each STRONG finding against **runtime state**. They:

- Run targeted commands to confirm the issue exists
- Check for config-vs-runtime discrepancies
- Assign severity (CRITICAL/HIGH/MEDIUM/LOW/FALSE-POSITIVE)
- Provide a specific fix and rollback plan

### Phase 3.5 — Weak Triage

WEAK MUST-VERIFY findings get batch-verified: one agent per domain runs quick commands
to confirm or dismiss each finding.

### Final Report

The manager compiles everything into `report.md` with:

- Executive summary with severity counts
- Findings grouped by severity
- False positives with explanations
- Parked observations for future investigation
- Model coverage analysis (which model found what)
- Pipeline statistics

## Key Design Decisions

1. **Separation of concerns:** Workers collect, evaluators analyze, verifiers confirm.
   No single agent does everything.

2. **Dual-model redundancy:** Running Sonnet and Opus independently catches more issues
   than either model alone. In testing, ~15% of HIGH/CRITICAL findings were caught by
   only one model.

3. **Evaluator layer:** Prevents the manager from losing low-priority findings when
   merging 800+ facts. The evaluator sees only its domain's data with a clean context.

4. **File-based coordination:** All data flows through files on disk. This makes the
   pipeline resumable and debuggable — every agent's exact command line is recorded.

5. **Regression checking:** Comparing against previous audits prevents findings from
   being lost across runs due to model non-determinism.
