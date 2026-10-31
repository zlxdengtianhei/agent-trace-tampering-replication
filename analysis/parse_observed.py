#!/usr/bin/env python3
"""Parse the two independent records of a run into one normalized event list per source.

Sources:
  strace  observe/strace.log  (strace -f -ttt -yy inside the container; container pid namespace)
  bpf     bpf.jsonl           (bpftrace -f json on the host; host pid namespace)

Normalized event (dict): src, t (epoch seconds), pid (process = thread group), tid, ppid, kind, plus
  exec:     path, argv          exec_fail: path, argv, ret
  open:     path, flags (str or int), ret       unlink/rmdir: path, ret
  rename:   path (old), path2 (new), ret        truncate: path, length
  connect:  family, addr, port, ret             exit: code (exit status, or "SIGxxx")
  fork:     child (strace only)                 kill: target, sig
The pid namespaces differ, so events from the two sources are never mixed in one process tree.

Usage: parse_observed.py <run_dir> [--src strace|bpf]   prints JSON lines
"""
import datetime
import json
import os
import re
import sys

LINE = re.compile(r"^(\d+)\s+(\d+\.\d+)\s(.*)$")
O_FLAGS = {"O_WRONLY": 0o1, "O_RDWR": 0o2, "O_CREAT": 0o100, "O_TRUNC": 0o1000, "O_APPEND": 0o2000}


# ---------- strace ----------

def split_top(s):
    """Split an strace argument list at top-level commas (outside quotes and brackets)."""
    out, cur, depth, q, esc = [], [], 0, False, False
    for ch in s:
        if q:
            cur.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                q = False
            continue
        if ch == '"':
            q = True
        elif ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur).strip())
    return out


def unquote(tok):
    """Decode the first strace C-string literal in tok; None if there is none."""
    m = re.search(r'"((?:[^"\\]|\\.)*)"', tok or "")
    if not m:
        return None
    raw = m.group(1)
    try:
        return bytes(raw, "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    except Exception:
        return raw


def fd_path(tok):
    """'3</a/b>' or 'AT_FDCWD</a>' -> '/a/b'; sockets and missing annotations -> None."""
    m = re.match(r"^[\w-]+<(/.*)>$", tok or "")
    return m.group(1) if m else None


def resolve(dirtok, pathtok):
    p = unquote(pathtok)
    if p is None:
        return None
    if p.startswith("/"):
        return os.path.normpath(p)
    base = fd_path(dirtok) if dirtok else None
    return os.path.normpath(os.path.join(base, p)) if base else p


def parse_ret(ret):
    ret = ret.strip()
    m = re.match(r"^(-?\d+|\?)(?:<([^>]*)>)?(?:\s+(\w+))?", ret)
    if not m:
        return ret, None, None
    val = m.group(1)
    return (int(val) if val != "?" else None), m.group(2), m.group(3)


def parse_sockaddr(tok):
    if "AF_INET6" in tok:
        a = re.search(r'inet_pton\(AF_INET6,\s*"([^"]+)"', tok)
        p = re.search(r"sin6_port=htons\((\d+)\)", tok)
        return "inet6", a.group(1) if a else None, int(p.group(1)) if p else None
    if "AF_INET" in tok:
        a = re.search(r'inet_addr\("([^"]+)"\)', tok)
        p = re.search(r"sin_port=htons\((\d+)\)", tok)
        return "inet", a.group(1) if a else None, int(p.group(1)) if p else None
    if "AF_UNIX" in tok:
        return "unix", unquote(tok), 0
    return "other", None, None


SELF_FD = re.compile(r"^/proc/(?:self|thread-self)/fd/(\d+)(/.*)?$")


def self_fd(p, pid, fds):
    """'/proc/self/fd/13/x' -> '<path of fd 13>/x'. Claude Code (Bun) opens the target directory with
    O_PATH and writes the temp file and the rename through /proc/self/fd/<n>/."""
    m = SELF_FD.match(p)
    base = fds.get((pid, int(m.group(1))))
    return os.path.normpath(base + (m.group(2) or "")) if base else p


def strace_events(path):
    events, pending, tgid, fds = [], {}, {}, {}
    if not os.path.exists(path):
        return events
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE.match(line.rstrip("\n"))
            if not m:
                continue
            tid, t, rest = int(m.group(1)), float(m.group(2)), m.group(3)
            pid = tgid.get(tid, tid)
            if rest.startswith("+++ exited with "):
                events.append({"src": "strace", "t": t, "pid": pid, "tid": tid, "kind": "exit",
                               "code": int(rest.split()[3])})
                continue
            if rest.startswith("+++ killed by "):
                events.append({"src": "strace", "t": t, "pid": pid, "tid": tid, "kind": "exit",
                               "code": rest.split()[3]})
                continue
            if rest.endswith("<unfinished ...>"):
                pending[tid] = (t, rest[: -len("<unfinished ...>")].rstrip())
                continue
            rm = re.match(r"^<\.\.\. (\w+) resumed>(.*)$", rest)
            if rm:
                if tid not in pending:
                    continue
                t, head = pending.pop(tid)
                rest = head + rm.group(2)
            cm = re.match(r"^(\w+)\((.*)\)\s+=\s+(.*)$", rest)
            if not cm:
                continue
            name, args, ret = cm.group(1), split_top(cm.group(2)), cm.group(3)
            rval, rpath, errno = parse_ret(ret)
            ev = {"src": "strace", "t": t, "pid": pid, "tid": tid, "syscall": name, "ret": rval}
            if errno:
                ev["errno"] = errno
            if name in ("execve", "execveat"):
                i = 1 if name == "execveat" else 0
                argv_tok = args[i + 1] if len(args) > i + 1 else "[]"
                argv = [unquote(x) or "" for x in split_top(argv_tok.strip()[1:-1])] if argv_tok.startswith("[") else []
                ev.update(kind="exec" if rval == 0 else "exec_fail", path=unquote(args[i]), argv=argv)
            elif name in ("clone", "clone3", "fork", "vfork"):
                if isinstance(rval, int) and rval > 0:
                    if "CLONE_THREAD" in cm.group(2):
                        tgid[rval] = pid
                        continue
                    ev.update(kind="fork", child=rval)
                else:
                    continue
            elif name in ("open", "creat"):
                ev.update(kind="open", path=resolve(None, args[0]), flags=args[1] if len(args) > 1 else "O_WRONLY|O_CREAT|O_TRUNC")
            elif name in ("openat", "openat2"):
                flags = args[2] if len(args) > 2 else ""
                if name == "openat2":
                    fm = re.search(r"flags=([\w|]+)", flags)
                    flags = fm.group(1) if fm else flags
                ev.update(kind="open", path=resolve(args[0], args[1]), flags=flags)
                if rpath and rpath.startswith("/"):
                    ev["path"] = rpath
            elif name in ("unlink", "rmdir"):
                ev.update(kind=name, path=resolve(None, args[0]))
            elif name == "unlinkat":
                ev.update(kind="rmdir" if "AT_REMOVEDIR" in (args[2] if len(args) > 2 else "") else "unlink",
                          path=resolve(args[0], args[1]))
            elif name == "rename":
                ev.update(kind="rename", path=resolve(None, args[0]), path2=resolve(None, args[1]))
            elif name in ("renameat", "renameat2"):
                ev.update(kind="rename", path=resolve(args[0], args[1]), path2=resolve(args[2], args[3]))
            elif name == "truncate":
                ev.update(kind="truncate", path=resolve(None, args[0]), length=args[1] if len(args) > 1 else None)
            elif name in ("ftruncate", "fallocate"):
                ev.update(kind="truncate" if name == "ftruncate" else "fallocate", path=fd_path(args[0]),
                          fd=args[0], length=args[1] if len(args) > 1 else None)
            elif name in ("link", "symlink"):
                ev.update(kind=name, path=resolve(None, args[0]), path2=resolve(None, args[1]))
            elif name in ("linkat", "symlinkat"):
                ev.update(kind=name[:-2], path=resolve(args[0] if name == "linkat" else None, args[0 if name == "symlinkat" else 1]),
                          path2=resolve(args[-3] if name == "linkat" else args[1], args[-2] if name == "linkat" else args[2]))
            elif name in ("mkdir", "mkdirat"):
                ev.update(kind="mkdir", path=resolve(args[0], args[1]) if name == "mkdirat" else resolve(None, args[0]))
            elif name in ("chmod", "fchmodat", "fchmodat2", "fchmod", "utimensat"):
                ev.update(kind="meta", path=resolve(args[0], args[1]) if name in ("fchmodat", "fchmodat2", "utimensat")
                          else (resolve(None, args[0]) if name == "chmod" else fd_path(args[0])))
            elif name == "connect":
                fam, addr, port = parse_sockaddr(args[1] if len(args) > 1 else "")
                ev.update(kind="connect", family=fam, addr=addr, port=port)
            elif name == "bind":
                fam, addr, port = parse_sockaddr(args[1] if len(args) > 1 else "")
                ev.update(kind="bind", family=fam, addr=addr, port=port)
            elif name in ("kill", "tgkill"):
                ev.update(kind="kill", target=args[-2] if name == "tgkill" else args[0], sig=args[-1])
            elif name == "exit_group":
                continue  # the "+++ exited with" line carries the same code
            else:
                ev.update(kind=name)
            for k in ("path", "path2"):
                if isinstance(ev.get(k), str) and SELF_FD.match(ev[k]):
                    ev[k] = self_fd(ev[k], pid, fds)
            if ev.get("kind") in ("truncate", "fallocate") and not ev.get("path") and str(ev.get("fd", "")).isdigit():
                # strace's -yy does not annotate every fd: take the path of this thread's latest open of that fd
                # (close() is not traced, so the path is inferred).
                guess = fds.get((pid, int(ev["fd"])))
                if guess:
                    ev.update(path=guess, path_inferred=True)
            if ev.get("kind") == "open" and isinstance(rval, int) and rval >= 0 and ev.get("path"):
                fds[(pid, rval)] = ev["path"]  # close() is not traced: the latest open of an fd number wins
            events.append(ev)
    return events


# ---------- bpftrace ----------

def iso_to_epoch(s):
    return datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc).timestamp()


def comps_to_path(comps, run_id):
    """Map the dentry components printed by bpftrace (leaf last) to a container path."""
    parts = [c for c in comps.split("/") if c]
    if run_id in parts:
        parts = parts[parts.index(run_id) + 1:]
        if parts and parts[0] == "home":
            return "/home/agent/" + "/".join(parts[1:])
        if parts and parts[0] == "workspace":
            return "/workspace/" + "/".join(parts[1:])
    for anchor, prefix in (("home", "/home/agent/"), ("workspace", "/workspace/")):
        if anchor in parts:
            i = len(parts) - 1 - parts[::-1].index(anchor)
            return prefix + "/".join(parts[i + 1:])
    return ".../" + "/".join(parts)


def bpf_events(path, run_id):
    events = []
    if not os.path.exists(path):
        return events
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "printf":
                continue
            fl = rec["data"].rstrip("\n").split("\t")
            k = fl[0]
            if k in ("BEGIN", "END") or len(fl) < 5:
                continue
            ev = {"src": "bpf", "t": iso_to_epoch(fl[1]), "pid": int(fl[2]), "tid": int(fl[3])}
            if k == "EXECFAIL":
                ev.update(kind="exec_fail", ret=int(fl[4]))
                events.append(ev)
                continue
            ev.update(ppid=int(fl[4]), comm=fl[5])
            if k == "EXEC":
                argv = []
                for a in fl[7:15]:
                    if a == "":
                        break
                    argv.append(a)
                ev.update(kind="exec", path=fl[6], argv=argv)
            elif k == "FORK":
                ev.update(kind="fork", child=int(fl[6]))
            elif k == "OPEN":
                ev.update(kind="open", flags=int(fl[6]), path=fl[7])
            elif k in ("UNLINK", "RMDIR"):
                ev.update(kind=k.lower(), ret=int(fl[6]), path=comps_to_path(fl[7], run_id))
            elif k == "RENAME":
                ev.update(kind="rename", path=comps_to_path(fl[7], run_id), path2=comps_to_path(fl[8], run_id))
            elif k == "TRUNCATE":
                ev.update(kind="truncate", length=int(fl[6]), path=fl[7])
            elif k == "FTRUNCATE":
                ev.update(kind="truncate", length=int(fl[6]), path=comps_to_path(fl[7], run_id))
            elif k == "CONNECT":
                ev.update(kind="connect", ret=int(fl[6]), family=fl[7], addr=fl[8], port=int(fl[9]))
            elif k == "EXIT":
                code = int(fl[6])
                ev.update(kind="exit", code=(code >> 8) & 0xFF if code & 0x7F == 0 else f"SIG{code & 0x7F}")
            else:
                continue
            events.append(ev)
    # The exec event fires in the old program; the kernel keeps the pid, so a failed exec is followed by EXECFAIL.
    out = []
    for i, e in enumerate(events):
        if e["kind"] == "exec":
            nxt = next((x for x in events[i + 1:i + 200] if x["tid"] == e["tid"]), None)
            if nxt and nxt["kind"] == "exec_fail":
                e = dict(e, kind="exec_fail", ret=nxt["ret"])
        if e["kind"] == "exec_fail" and "argv" not in e:
            continue
        out.append(e)
    return out


def flags_value(flags):
    if isinstance(flags, int):
        return flags
    v = 0
    for name, bit in O_FLAGS.items():
        if name in (flags or ""):
            v |= bit
    return v


def is_write_open(flags):
    v = flags_value(flags)
    return bool(v & (O_FLAGS["O_WRONLY"] | O_FLAGS["O_RDWR"] | O_FLAGS["O_CREAT"] | O_FLAGS["O_TRUNC"]))


def load(run_dir):
    run_id = os.path.basename(os.path.normpath(run_dir))
    return {"strace": strace_events(os.path.join(run_dir, "observe", "strace.log")),
            "bpf": bpf_events(os.path.join(run_dir, "bpf.jsonl"), run_id)}


if __name__ == "__main__":
    srcs = load(sys.argv[1])
    want = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--src" else None
    for name, evs in srcs.items():
        if want and name != want:
            continue
        for e in evs:
            print(json.dumps(e))
