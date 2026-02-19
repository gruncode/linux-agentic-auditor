# Ensemble System Auditor v3

> **Usage:** Copy the content below (starting from `SYSTEM AUDIT`) and paste it as your
> prompt in Claude Code. The MCP spawn-agent server must be running (see `examples/mcp-config.json`).
>
> Before running, set the `AUDIT_DIR` variable in your head — the prompt uses `~/audits/YYYY-MM-DD/`
> by default. You can customize the audit directory, domains, wave sizes, and models to fit your needs.

---

SYSTEM AUDIT — ENSEMBLE MODEL v3 (DUAL-MODEL DISCOVERY + EVALUATOR LAYER + OPUS VERIFICATION)

You are the ONLY manager. Use MCP tools spawn_batch/spawn_agent to delegate.

DESIGN RATIONALE
This prompt runs two models (Sonnet + Opus) for Phase 1 discovery, then adds
a per-domain EVALUATOR layer that analyzes both models' outputs with a clean
context window. This solves the v1 ensemble's main weakness: the manager
generating findings from 832+ merged facts lost lower-priority observations
(7 findings dropped including a MEDIUM-severity OOM-kills-screen-locker).

v3 changes from v2:
- Audits saved to persistent dated directories under ~/audits/
- Regression check against previous audit (prevents cross-run finding loss)
- WEAK MUST-VERIFY findings now get batch triage verification (Phase 3.5)
- Explicit severity anchors for verifier calibration consistency
- Related findings may be grouped in verifier agents (saves agent budget)
- Strict verify.md header format (eliminates parse failures)
- Parking promotion threshold raised from 35 to 60

Pipeline: Workers COLLECT -> Evaluators ANALYZE -> Manager COORDINATES -> Verifiers CONFIRM -> Triage WEAK
Each agent type does one thing well. Analytical judgment happens at the point
closest to the data (evaluators), not at the manager level.

AUDIT DIRECTORY
- All output goes to: ~/audits/YYYY-MM-DD/
  (use today's date, e.g., ~/audits/2026-02-20/)
- If that directory already exists (re-run on same day), append a suffix:
  ~/audits/YYYY-MM-DD-2/, YYYY-MM-DD-3/, etc.
- Previous audits live in sibling directories. The manager will scan
  ~/audits/ to find the most recent completed audit for
  regression checking in Phase 2.
- Variable: $AUDIT_DIR refers to the current audit's directory throughout.

HARD CONSTRAINTS (MUST FOLLOW)
- You MUST use MCP tools spawn_batch/spawn_agent to run domain work.
- You MUST NOT replace domain agents with bash scripts, GNU parallel, tmux, xargs -P, or background jobs.
- Do NOT create collect.sh (or equivalent) to run the whole audit outside the worker agents.
- Only the workers run the domain commands. Evaluators and verifiers may run targeted commands.
- The manager only coordinates, reads evaluator outputs, deduplicates, and spawns verifiers.
- If a worker task is too long, split into more workers or more waves — still via MCP.

ABSOLUTE RULES
- All domain agents are workers: role=auditor (role=log-analyst for logs), depth_limit=0.
- Everything is written to $AUDIT_DIR (files on disk).
- Every worker must create:
  - $AUDIT_DIR/phase1/NN-domain-MODEL/commands.sh
  - $AUDIT_DIR/phase1/NN-domain-MODEL/raw/ (one file per command output)
  - $AUDIT_DIR/phase1/NN-domain-MODEL/index.json (facts + candidates + pointers to raw files)
  where MODEL is "sonnet" or "opus" (e.g., 01-processes-sonnet/, 01-processes-opus/)
- Use sudo for commands. Use timeouts for risky commands (timeout 10s/15s sudo ...).
- After each wave/phase, update $AUDIT_DIR/state.json with progress so this run can resume.

===============================================
PHASE 1 — DUAL-MODEL DISCOVERY (facts + candidates)
===============================================

Domains to audit:
01 Processes/Resources (top, ps aux, lsof, /proc/meminfo, OOM config, swap and others)
02 Systemd services/timers/cron (failed units, boot performance, enabled vs running, cron/at jobs, and others)
03 Network (listening ports, firewall rules, established connections, DNS, routing, interfaces, and others)
04 Storage/Filesystems (LUKS status, fstab vs actual mounts, SMART full data incl. unsafe shutdown counts, df, lsblk, tune2fs intervals, and others)
05 System config (GRUB cmdline, runtime sysctl vs defaults, loaded modules vs blacklisted, PAM config, sudoers AND /etc/sudoers.d/ contents, login.defs, and others)
06 Installed software (packages, repos, apt timers, pending security updates, docker, VMs, unattended-upgrades status, and others)
07 Hardware/User environment (lshw, sensors, CPU governor, user accounts, home dir permissions, SSH keys/config, .bash_history perms, crontabs, group memberships, and others)
08 Log analysis (journalctl disk usage + retention settings, rsyslog presence, dmesg errors, auth log patterns, recent boot timeline, segfault patterns, log rotation status, and others; still facts-first)

Each domain gets TWO workers — one model=sonnet, one model=opus.
That is 16 workers total (8 domains x 2 models).

Spawn in waves of 3 workers at a time (spawn_batch MAX_PARALLEL=3):
  Wave 1: 01-processes-sonnet, 01-processes-opus, 02-systemd-sonnet
  Wave 2: 02-systemd-opus, 03-network-sonnet, 03-network-opus
  Wave 3: 04-storage-sonnet, 04-storage-opus, 05-sysconfig-sonnet
  Wave 4: 05-sysconfig-opus, 06-software-sonnet, 06-software-opus
  Wave 5: 07-hardware-sonnet, 07-hardware-opus, 08-logs-sonnet
  Wave 6: 08-logs-opus

Each worker task must:
- create its directory (e.g., $AUDIT_DIR/phase1/01-processes-sonnet/), commands.sh, raw/
- run commands with outputs redirected to raw/*.txt
- write index.json with structured facts + candidates + mapping to raw files
- Workers for the SAME domain are independent — they may run the same commands
  or different ones. This redundancy is intentional for coverage.

WORKER OUTPUT FORMAT — index.json must contain BOTH facts AND candidates:

{
  "domain": "NN-domain",
  "model": "sonnet|opus",
  "facts": [
    {
      "topic": "descriptive_topic_name",
      "value": "observed value or state",
      "raw_file": "raw/filename.txt"
    }
  ],
  "candidates": [
    {
      "topic": "matching_fact_topic",
      "signal": "STRONG|WEAK",
      "observation": "one-line description of what looks anomalous or noteworthy"
    }
  ]
}

CANDIDATE RULES FOR WORKERS:
- Workers must flag EVERY anomaly they observe as a candidate. No minimum, no maximum.
- STRONG signal: clear deviation from best practice, security gap, error state,
  missing security tool, misconfiguration with measurable impact.
- WEAK signal: cosmetic issue, stale config, minor inconsistency, "looks odd but
  might be intentional", maintenance debt, unusual but not clearly harmful.
- Do NOT assign severity. Do NOT write recommendations. Just flag and describe.
- When in doubt, flag it as WEAK. It is better to over-flag than to miss something.
  The evaluator and verifier will filter — that is their job, not the worker's.
- A worker typically produces 8-20 candidates. Fewer is fine if the domain is clean.
  More is fine if the domain has many anomalies.

===============================================
PHASE 1.5 — DOMAIN EVALUATORS (one Opus agent per domain)
===============================================

After all 16 workers complete, spawn 8 EVALUATOR agents (one per domain).
Each evaluator reads BOTH workers' outputs for its domain and produces a
ranked, deduplicated candidate list.

Evaluators use: model=opus, role=auditor, depth_limit=0, timeout=600

Spawn in waves of 3 (same MAX_PARALLEL=3 constraint):
  Wave 7: eval-01-processes, eval-02-systemd, eval-03-network
  Wave 8: eval-04-storage, eval-05-sysconfig, eval-06-software
  Wave 9: eval-07-hardware, eval-08-logs

Each evaluator task prompt must include:
1. The FULL contents of both workers' index.json files for this domain
   (copy-paste the JSON into the evaluator's task prompt so it has the data)
2. The evaluator's analysis instructions (below)

EVALUATOR INSTRUCTIONS (include in each evaluator's task prompt):

You are a security evaluator for the {DOMAIN} domain of a Linux system.

Below are facts and candidates from two independent audit workers (Sonnet
and Opus) that examined the same system independently.

Your task:
1. Read ALL facts from both workers. Note where they agree, disagree, or
   where one found something the other missed.
2. Read ALL candidates from both workers. These are the workers' own
   assessments of what looks anomalous.
3. Perform your OWN analysis of the facts. Look for:
   - Security gaps or misconfigurations
   - Reliability/stability risks
   - Maintenance debt or configuration drift
   - Resource waste (open but unused mappings, unrotated logs, stale services)
   - Missing security infrastructure (fail2ban, auditd, logrotate configs, etc.)
   - Attack chains: how could multiple observations combine to escalate access?
4. Produce a RANKED candidate list with signal strength:
   - STRONG: clear risk, deviation from best practice, measurable impact,
     or flagged by both workers
   - WEAK: cosmetic, stale config, minor oddity, flagged by only one worker
     with low confidence
5. For EACH candidate, note which worker(s) flagged it: both, sonnet, opus,
   or "evaluator" if you identified it yourself from the facts.
6. Do NOT drop ANY candidate that either worker flagged as STRONG.
7. Do NOT assign severity ratings. That is the verifier's job.

IMPORTANT: Do NOT run any shell commands. Do NOT use sudo. Do NOT verify
data live. Your ONLY task is to ANALYZE the provided facts and candidates,
then WRITE the output JSON file.

Think carefully about cross-fact patterns within this domain. Two individually
minor facts may together indicate a significant issue. For example:
- "OOM killed a service" + "that service was a screen locker" = security breach
- "file owned by non-root user" + "file is in /etc/modprobe.d/" = privilege risk
- "log file is 400MB" + "no logrotate config exists" = disk exhaustion risk

EVALUATOR OUTPUT: Write to $AUDIT_DIR/phase1/NN-domain-evaluated/candidates.json

{
  "domain": "NN-domain",
  "evaluator_model": "opus",
  "sonnet_facts_count": N,
  "opus_facts_count": N,
  "sonnet_candidates_count": N,
  "opus_candidates_count": N,
  "evaluated_candidates": [
    {
      "id": "DOM-NNN",
      "title": "short descriptive title",
      "description": "what was observed and why it matters",
      "signal": "STRONG|WEAK",
      "flagged_by": "both|sonnet|opus|evaluator",
      "evidence_facts": ["topic1", "topic2"],
      "evidence_raw": ["path/to/raw/file1.txt", "path/to/raw/file2.txt"]
    }
  ],
  "cross_fact_patterns": [
    "description of any multi-fact patterns noticed"
  ]
}

===============================================
PHASE 2 — MANAGER MERGE AND FINDING ASSIGNMENT (manager task, no agents)
===============================================

After all 8 evaluators complete, the manager (you) must:

1. Read all 8 candidates.json files from $AUDIT_DIR/phase1/NN-domain-evaluated/
2. Collect ALL candidates from all evaluators into a single list.
3. Deduplicate across domains (some findings may appear in multiple domains —
   e.g., a network fact and a sysconfig fact about the same issue).
4. Assign finding IDs: FND-001, FND-002, etc.
5. Classify each finding for verification:
   - MUST-VERIFY: any STRONG candidate (regardless of source)
   - MUST-VERIFY: any WEAK candidate flagged by both workers or by evaluator
   - PARK: WEAK candidates flagged by only one worker, not reinforced by evaluator
6. Write $AUDIT_DIR/phase2/findings.json with this structure:
   {
     "findings": [
       {
         "id": "FND-XXX",
         "title": "...",
         "description": "...",
         "domain": "NN-domain",
         "signal": "STRONG|WEAK",
         "flagged_by": "both|sonnet|opus|evaluator",
         "verify": "MUST|PARK",
         "evidence_facts": [...],
         "evidence_raw": [...]
       }
     ],
     "parked": [
       {
         "id": "PARK-XXX",
         "title": "...",
         "description": "...",
         "domain": "NN-domain",
         "reason_parked": "WEAK signal from single model, not reinforced"
       }
     ]
   }

REGRESSION CHECK (run before parking rules):
- Scan ~/audits/ for the most recent sibling directory that
  contains a phase2/findings.json with findings (skip the current run).
- If found, load its findings.
- For each previous finding whose verify.md shows severity HIGH or CRITICAL:
  - Search current MUST-VERIFY list for a matching topic or title
    (fuzzy match on keywords — e.g., "segfault" or "drm.debug")
  - If NO match found in MUST-VERIFY or PARKED:
    force-add the previous finding as MUST-VERIFY with signal=STRONG,
    flagged_by="regression-check", and include the previous finding's
    description as evidence
- Log all regression-check additions to $AUDIT_DIR/phase2/regression-check.log
- This step is zero-cost (JSON comparison only, no agents needed)

PARKING RULES:
- Parked candidates are NOT lost. They appear in the final report appendix.
- If total MUST-VERIFY candidates are fewer than 60, promote PARKED candidates
  (highest signal first) until you reach 60 or exhaust the parked list.
- The parked list is a safety net: the user can request follow-up verification
  of parked items in a future audit session.

===============================================
PHASE 3 — VERIFIED ANALYSIS (Opus verifiers, STRONG findings)
===============================================

For each STRONG MUST-VERIFY finding, spawn verifiers:
- Prefer one finding per verifier for CRITICAL/HIGH candidates
- For MEDIUM/LOW: may group 2-4 closely related findings per verifier
  (same domain, same subsystem — e.g., SSH config findings together)
- Never group findings from different domains

Verifier config: model=opus, role=verifier, depth_limit=0, timeout=300
Spawn in waves of 3 (spawn_batch MAX_PARALLEL=3).

Each verifier:
- checks runtime state and config-vs-runtime discrepancies
- assigns severity using these anchors:
    CRITICAL: Active exploitation path exists NOW — public exposure, unpatched
              RCE, privilege escalation with proven exploit, data accessible
              without authentication from the network
    HIGH:     Exploitable with local access, missing critical defense layer,
              pending security updates with known CVEs, data integrity at risk
    MEDIUM:   Hardening gap, degraded reliability, missing recommended tooling,
              configuration drift with measurable but non-immediate impact
    LOW:      Best-practice deviation with minimal operational risk, cosmetic,
              stale config, informational
    FALSE-POSITIVE: Claimed issue does not exist at runtime
- writes:
  $AUDIT_DIR/phase2/FND-XXX/verify.md
  $AUDIT_DIR/phase2/FND-XXX/evidence/* (if needed)

Verifier output format requirement (MANDATORY for verify.md):
The FIRST lines of verify.md MUST use this EXACT format (plain text, not markdown table):

  # FND-XXX: <title>
  Severity: CRITICAL|HIGH|MEDIUM|LOW|FALSE-POSITIVE
  Verified: YES|NO|PARTIAL
  Source-Model: both|sonnet|opus|evaluator

Do NOT use markdown tables for the header. Do NOT use bold/italic in the
Severity line. The manager parses these lines programmatically.

Then write the verification narrative, Runtime Evidence, Fix, and Rollback sections.

===============================================
PHASE 3.5 — WEAK MUST-VERIFY TRIAGE (batch verification)
===============================================

After all STRONG verifiers complete, batch-verify the WEAK MUST-VERIFY findings.

Group WEAK findings by domain (all WEAK findings from the same domain go to
one agent). If a domain has 0 WEAK findings, skip it. If a domain has only
1-2 WEAK findings, combine with an adjacent domain's WEAK findings.

Each triage agent:
- model=opus, role=auditor, depth_limit=0, timeout=300
- Receives ALL WEAK MUST-VERIFY findings for its domain(s)
- For each finding: run ONE quick command to confirm or dismiss
- Writes a SINGLE verify.md per finding using the same header format as Phase 3
- If a finding upgrades to MEDIUM+ severity after verification, flag it explicitly
- If a finding is clearly noise/cosmetic, mark as LOW or FALSE-POSITIVE
- Be concise: max 20 lines per verify.md

Include the same severity anchors from Phase 3 in the triage agent prompt.

Spawn in waves of 3 (same constraint). Expect ~4-6 triage agents total.

===============================================
FINAL — REPORT
===============================================

Write $AUDIT_DIR/phase2/report.md with these sections:

1. Executive Summary (severity counts, top risks)
2. Findings grouped by severity (CRITICAL -> HIGH -> MEDIUM -> LOW)
   - Each finding: title, verified status, evidence summary, fix, source attribution
3. Rejected Findings (FALSE-POSITIVE with explanation)
4. Parked Observations (unverified WEAK candidates for future investigation)
5. Model Coverage Analysis:
   - How many findings from both models, sonnet-only, opus-only, evaluator-discovered
   - Which model found the highest-severity unique findings
   - Evaluator cross-fact patterns that surfaced new candidates
6. Regression Check Results:
   - Findings carried forward from previous audit (if any)
   - Findings from previous audit that are now resolved
   - New findings not present in any previous audit
7. Pipeline Statistics (workers, evaluators, verifiers, triage agents, timing)

No system changes should be made. Wait for user approval before changes.

===============================================
BEGIN
===============================================

1. Create $AUDIT_DIR directory (e.g., ~/audits/YYYY-MM-DD/)
2. Initialize $AUDIT_DIR/state.json
3. Check for previous audit in ~/audits/ (for Phase 2 regression check)
   - Record previous audit path in state.json if found
4. Start Phase 1 using spawn_batch.

Use model=opus for Opus workers and model=sonnet for Sonnet workers.
All evaluators use model=opus. All verifiers use model=opus. All triage agents use model=opus.
Worker timeout: 1200 seconds. Evaluator timeout: 600 seconds.
Verifier timeout: 300 seconds. Triage agent timeout: 300 seconds.
