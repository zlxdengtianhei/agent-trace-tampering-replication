# s0_smoke — pipeline check with a real agent

Not one of the five scenarios. Two short turns check that the agent reaches the model through the
gateway bridge, that turn 2 continues the session of turn 1 (`--continue`), and that the two tool
calls of each turn appear in the trace, in strace and in bpftrace.
