---
name: privacy-financial-data
description: "User wants personal financial figures (income, debts) kept out of local traces and not restated in replies"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 6f2a8628-d710-428d-b03e-08d1c7954c0b
  modified: 2026-10-31T18:42:14.077Z
---

On 2026-10-31 the user asked to have their income and debt figures removed from the local Claude Code session logs (transcript JSONL under `~/.claude/projects/`), keeping everything else.

**Why:** privacy of personal financial data on this machine; derived totals can re-derive the originals, so they count too.

**How to apply:** when handling this user's financial data, avoid restating figures unnecessarily in replies or memory; if asked to redact traces again, scrub string values inside the JSONL (never mangle non-string metadata), and note that a still-open session keeps appending — a final re-scrub after session close is the belt-and-suspenders option.
