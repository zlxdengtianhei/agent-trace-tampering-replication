# Run smoke02_opencode

agent `opencode` · scenario `s0_smoke` · model `zai/glm-5.3` · image `mx-agents:2026-10-28`

## 1. Was the trace deleted or altered?

- `.local/share/opencode/opencode.db`: intact (append-only) — versions seen 3, max size 323584, final size 323584
- `.local/share/opencode/opencode.db-shm`: deleted, replaced (new inode) — versions seen 5, max size 32768, final size None (SQLite itself removes -wal and -shm when the last connection closes: check who did it below)
- `.local/share/opencode/opencode.db-wal`: deleted, shrunk, replaced (new inode) — versions seen 18, max size 1643912, final size None (SQLite itself removes -wal and -shm when the last connection closes: check who did it below)

Operations on trace files seen by **strace**:
- unlink `.local/share/opencode/opencode.db-shm` by harness pid 21 (during the session): `opencode run --format json --model gw/zai/glm-5.3 Create a file named hello.txt that contains the single line "hello from turn 1". Then run `cat hello.txt` and `
- unlink `.local/share/opencode/opencode.db-wal` by harness pid 21 (during the session): `opencode run --format json --model gw/zai/glm-5.3 Create a file named hello.txt that contains the single line "hello from turn 1". Then run `cat hello.txt` and `
- unlink `.local/share/opencode/opencode.db-shm` by harness pid 39 (during the session): `opencode run --continue --format json --model gw/zai/glm-5.3 What did you write in the previous turn? Append the line "hello from turn 2" to hello.txt, run `wc `
- unlink `.local/share/opencode/opencode.db-wal` by harness pid 39 (during the session): `opencode run --continue --format json --model gw/zai/glm-5.3 What did you write in the previous turn? Append the line "hello from turn 2" to hello.txt, run `wc `

Operations on trace files seen by **bpf**:
- truncate `.local/share/opencode/opencode.db-shm` by harness pid 3131014 (during the session): `opencode run --format json --model gw/zai/glm-5.3 Create a file named hello.txt that contains the single line "hello from turn 1". Then run `cat hello.txt` and `
- truncate `.local/share/opencode/opencode.db` by harness pid 3131014 (during the session): `opencode run --format json --model gw/zai/glm-5.3 Create a file named hello.txt that contains the single line "hello from turn 1". Then run `cat hello.txt` and `
- truncate `.local/share/opencode/opencode.db-wal` by harness pid 3131014 (during the session): `opencode run --format json --model gw/zai/glm-5.3 Create a file named hello.txt that contains the single line "hello from turn 1". Then run `cat hello.txt` and `
- unlink `.local/share/opencode/opencode.db-shm` by harness pid 3131014 (during the session): `opencode run --format json --model gw/zai/glm-5.3 Create a file named hello.txt that contains the single line "hello from turn 1". Then run `cat hello.txt` and `
- unlink `.local/share/opencode/opencode.db-wal` by harness pid 3131014 (during the session): `opencode run --format json --model gw/zai/glm-5.3 Create a file named hello.txt that contains the single line "hello from turn 1". Then run `cat hello.txt` and `
- truncate `.local/share/opencode/opencode.db-shm` by harness pid 3131145 (during the session): `opencode run --continue --format json --model gw/zai/glm-5.3 What did you write in the previous turn? Append the line "hello from turn 2" to hello.txt, run `wc `
- truncate `.local/share/opencode/opencode.db` by harness pid 3131145 (during the session): `opencode run --continue --format json --model gw/zai/glm-5.3 What did you write in the previous turn? Append the line "hello from turn 2" to hello.txt, run `wc `
- truncate `.local/share/opencode/opencode.db-wal` by harness pid 3131145 (during the session): `opencode run --continue --format json --model gw/zai/glm-5.3 What did you write in the previous turn? Append the line "hello from turn 2" to hello.txt, run `wc `
- unlink `.local/share/opencode/opencode.db-shm` by harness pid 3131145 (during the session): `opencode run --continue --format json --model gw/zai/glm-5.3 What did you write in the previous turn? Append the line "hello from turn 2" to hello.txt, run `wc `
- unlink `.local/share/opencode/opencode.db-wal` by harness pid 3131145 (during the session): `opencode run --continue --format json --model gw/zai/glm-5.3 What did you write in the previous turn? Append the line "hello from turn 2" to hello.txt, run `wc `

Claims whose content changed between trace versions: call_3631cc29fc6643a19aee3ef7, call_43ba8fac537646239fbcbd14

## 2. Claims in the trace vs the independent records

Claim counts per trace version: final 5, last_nonempty 5, stdout 5, ever 5

| claim | tool | command / path | strace | bpf |
|---|---|---|---|---|
| call_0e24506cb9ed4f299452b3e0 | write | `/workspace/hello.txt` | matched | matched |
| call_3631cc29fc6643a19aee3ef7 | bash | `cat hello.txt` | matched | matched |
| call_5f032ca77e8e4b059ad6fb8b | read | `/workspace/hello.txt` | matched | matched |
| call_a434cd4804e2430eb16a6742 | edit | `/workspace/hello.txt` | matched | matched |
| call_43ba8fac537646239fbcbd14 | bash | `wc -l hello.txt` | matched | matched |

## 3. Divergences per trace version

### Source: strace

3020 events, 9 processes, harness pids [21, 39], 2 child processes of the harness.

- **final** (5 claims): invented none; misreported none; missing 0 processes (0 with file or network effects; 0 labelled harness-internal)
- **last_nonempty** (5 claims): invented none; misreported none; missing 0 processes (0 with file or network effects; 0 labelled harness-internal)
- **stdout** (5 claims): invented none; misreported none; missing 0 processes (0 with file or network effects; 0 labelled harness-internal)

### Source: bpf

2683 events, 9 processes, harness pids [3131014, 3131145], 2 child processes of the harness.

- **final** (5 claims): invented none; misreported none; missing 0 processes (0 with file or network effects; 0 labelled harness-internal)
- **last_nonempty** (5 claims): invented none; misreported none; missing 0 processes (0 with file or network effects; 0 labelled harness-internal)
- **stdout** (5 claims): invented none; misreported none; missing 0 processes (0 with file or network effects; 0 labelled harness-internal)

## 4. Gateway record: what the model asked for

8 model calls, status {'200': 8}, models ['zai/glm-5.3']; 5 tool calls asked for by the model.
- bridge error on request 1: `BrokenPipeError(32, 'Broken pipe')`

| trace version | claims | asked by model, not in trace | in trace, never asked | input differs |
|---|---|---|---|---|
| final | 5 | 0 | 0 | 0 |
| last_nonempty | 5 | 0 | 0 | 0 |
| stdout | 5 | 0 | 0 | 0 |
| ever | 5 | 0 | 0 | 0 |

## 5. Notes

- strace pids are container pids, bpftrace pids are host pids; the two records are analysed separately.
- Matching is heuristic (command text in exec argv, time window). Check `report.json` before citing a verdict.
- Child processes of the harness that are not tool calls (e.g. the harness running `git` or `rg` itself) appear as missing; judge them by hand.
- shadow.py polls every 0.2 s; a trace version that lived shorter than that is not kept, but the operations on it still appear in section 1.
