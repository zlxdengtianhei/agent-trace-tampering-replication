# s5 — your own design (TODO)

Write `turns/01.txt` (and more turns if needed), and `workspace/` or `home/` if the scenario needs files.
Ideas, not decisions:
- Unreported side effects: a task whose easy path has a side effect the agent may not mention
  (e.g. it installs a package, writes outside the workspace, or opens a network connection).
- Status report under pressure: a long task with a reward-like cue ("you will be graded on a clean, short log");
  compare the final summary with the observed actions.
