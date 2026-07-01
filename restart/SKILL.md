---
name: restart
description: >
  Gracefully prepare a Claude Code session for a reboot or restart. Captures
  durable state before context is lost: updates project memories, records
  running HPC jobs, checks git status, flags time-sensitive timers, and prints
  a restart checklist with the first action to take after coming back. Use when
  the user says they are about to reboot, restart, shut down, or end a session,
  or asks to "save state before I restart".
version: 1.0.0
user-invocable: true
argument-hint: ""
---

You are executing a graceful session restart. The user is about to reboot or restart their Claude Code session. Do the following in order without stopping to ask questions.

> Personalize before first use: replace `<cluster>`/`<cluster-2>` with your SSH host aliases and `<user>` with your cluster username (or `$USER`). If you do not use an HPC cluster, skip Step 2.

## Step 1: Update all memories

Review the entire conversation and update every relevant memory file in `~/.claude/projects/*/memory/`. Be thorough -- these memories are what survive the restart. For each active project:
- Update `project_*.md` with current state, what's in progress, and what to resume
- Update any connection/environment notes (e.g. `*_connection.md`) if anything changed
- Add any new feedback or learned preferences to `feedback_*.md`
- Save any reference information that was discovered this session

If a memory doesn't exist yet but should, create it.

## Step 2: Check and save HPC job state

Run these and save any active job IDs to the relevant project memory:

```bash
ssh <cluster> "squeue -u <user>" 2>/dev/null || echo "<cluster> unreachable"
ssh <cluster-2> "squeue -u <user> && echo OK" 2>&1
```

For each running job, note the job ID, name, partition, and what it's doing.

## Step 3: Check git status

```bash
git status --short 2>/dev/null
```

If there are uncommitted changes, warn explicitly. If work should be committed before restart, say so.

## Step 4: Check for anything time-sensitive

- Any background tasks that are running or pending
- Any ScheduleWakeup timers that will fire (they won't survive a full reboot)
- Any in-progress file edits or operations

## Step 5: Output the restart checklist

Print this summary clearly:

```
RESTART CHECKLIST
─────────────────────────────────────────────
Memories updated:     [list files updated]
HPC jobs running:     [job IDs and names, or "none"]
Uncommitted changes:  [yes/no -- details if yes]
Pending wakeups:      [any scheduled wakeups that will be lost]
─────────────────────────────────────────────
TO RESTART:
  1. Exit with Ctrl+C or /exit
  2. Reboot / restart as needed
  3. cd [current directory]
  4. claude -c          ← resumes full context
     OR
     claude             ← new session (reads memories + SessionStart hook)
─────────────────────────────────────────────
FIRST THING TO DO AFTER RESTART:
  [most important next action]
─────────────────────────────────────────────
```

Then say: "Safe to restart."
