#!/usr/bin/env python3
# Code Duo — one window driving Claude and Antigravity (Gemini); resume past sessions, hand off, audit.
# No API: drives the claude / agy CLIs, authenticated with your subscriptions.
import json, subprocess, threading, time, os, glob, re, shutil, platform
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
SYS = platform.system()


# ---------- auto-detect CLIs and session locations (cross-platform, env-overridable) ----------
def _first_dir(paths):
    for p in paths:
        if p and os.path.isdir(p):
            return p
    return None


def _which(name, extra):
    return shutil.which(name) or next((p for p in extra if p and os.path.exists(p)), None)


def discover():
    # CLI binaries
    claude_bin = os.environ.get("DUO_CLAUDE_BIN") or _which("claude", [
        "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
        os.path.join(HOME, ".local/bin/claude"), os.path.join(HOME, ".npm-global/bin/claude"),
        os.path.join(HOME, ".bun/bin/claude")])
    agy_bin = os.environ.get("DUO_AGY_BIN") or _which("agy", [
        os.path.join(HOME, ".local/bin/agy"),
        "/opt/homebrew/bin/agy", "/usr/local/bin/agy"])

    # Claude CLI config dir (override with CLAUDE_CONFIG_DIR) -> projects/
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    claude_proj = os.path.join(cfg, "projects")

    # Claude desktop app session index (clean titles; optional, None if not installed -> fall back to jsonl)
    if SYS == "Darwin":
        sess_cands = [os.path.join(HOME, "Library/Application Support/Claude/claude-code-sessions")]
    elif SYS == "Windows":
        sess_cands = [os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code-sessions")]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
        sess_cands = [os.path.join(xdg, "Claude", "claude-code-sessions")]
    claude_sess = _first_dir(sess_cands)

    # Antigravity CLI home (override with ANTIGRAVITY_CLI_DIR); the agy CLI reuses the ~/.gemini tree
    ag = os.environ.get("ANTIGRAVITY_CLI_DIR") or os.path.join(HOME, ".gemini", "antigravity-cli")

    return {
        "claude_bin": claude_bin, "agy_bin": agy_bin,
        "claude_proj": claude_proj, "claude_sess": claude_sess,
        "agy_home": ag,
        "agy_conversations": os.path.join(ag, "conversations"),
        "agy_history": os.path.join(ag, "history.jsonl"),
        "agy_last": os.path.join(ag, "cache", "last_conversations.json"),
        "agy_projects": os.path.join(ag, "cache", "projects.json"),
    }


CFG = discover()
CLAUDE_BIN = CFG["claude_bin"]
AGY_BIN = CFG["agy_bin"]
CLAUDE_PROJ = CFG["claude_proj"]
CLAUDE_SESS = CFG["claude_sess"]
AGY_CONVS = CFG["agy_conversations"]

# each agent remembers which session it's resuming + which dir to run in (resume is cwd-bound)
STATE = {"claude": {"id": None, "cwd": HERE}, "antigravity": {"id": None, "cwd": HERE}}
# shared setting: the real project directory both agents work in
SETTINGS = {"project": None}
# per-agent model / mode (like the controls at the bottom of Claude Code).
# agy bakes reasoning effort into the model variant (e.g. "Gemini 3.1 Pro (High)"), so there is no separate effort.
AGENT_CFG = {
    "claude": {"model": "", "mode": "default", "effort": "", "fast": False},
    "antigravity": {"model": "", "mode": "sandbox"},
}
# which modes can write files (used by the watchdog to decide whether to diff the disk)
_WRITABLE = {"acceptEdits", "bypassPermissions", "auto", "dontAsk", "skip-permissions"}
LOCK = threading.Lock()

# USD price per million tokens (from mercury-cache-panel). Only Claude exposes per-turn usage on disk.
PRICING = {
    "claude": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
}
# watchdog: recent claim-vs-evidence checks (newest first)
BEHAVIOR = []
# loop detection: per-agent count of consecutive 'claimed actions but 0 disk change' turns
STREAK = {"claude": 0, "antigravity": 0}
_TOK_CACHE = {"ts": 0, "data": None}

# Code Duo's own overrides (rename/pin/archive/delete); never touches the official apps' data
OV_PATH = os.path.join(HERE, "duo_overrides.json")


def _load_overrides():
    try:
        return json.load(open(OV_PATH))
    except Exception:
        return {}


def _save_overrides(d):
    json.dump(d, open(OV_PATH, "w"), ensure_ascii=False, indent=0)


def _apply_overrides(engine, rows, include_hidden=False):
    ov = _load_overrides().get(engine, {})
    out = []
    for r in rows:
        o = ov.get(r["id"], {})
        if (o.get("deleted") or o.get("archived")) and not include_hidden:
            continue
        if o.get("title"):
            r["title"] = o["title"]
        r["pinned"] = bool(o.get("pinned"))
        r["archived"] = bool(o.get("archived"))
        r["deleted"] = bool(o.get("deleted"))
        out.append(r)
    out.sort(key=lambda r: (not r["pinned"], -r["ts"]))
    return out
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def run(cmd, cwd, timeout=600):
    p = subprocess.run(cmd, cwd=cwd or HERE, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


# ---------- drive the two engines ----------
def ask_claude(text):
    t0 = time.time()
    if not CLAUDE_BIN:
        return {"ok": False, "text": "[claude CLI not found] Install Claude Code, or set DUO_CLAUDE_BIN to its path",
                "ms": 0, "meta": {}}
    with LOCK:
        sid, cwd = STATE["claude"]["id"], STATE["claude"]["cwd"]
        cfg = dict(AGENT_CFG["claude"])
    cmd = [CLAUDE_BIN, "-p", text, "--output-format", "json"]
    if sid:
        cmd += ["--resume", sid]
    cmd += ["--permission-mode", cfg["mode"] or "default"]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]
    if cfg["effort"]:
        cmd += ["--effort", cfg["effort"]]
    cmd += ["--settings", json.dumps({"fastMode": bool(cfg.get("fast"))})]
    rc, out, err = run(cmd, cwd)
    try:
        d = json.loads(out)
        new_sid = d.get("session_id")
        if new_sid:
            with LOCK:
                STATE["claude"]["id"] = new_sid
        return {"ok": not d.get("is_error"), "text": d.get("result", ""),
                "ms": int((time.time()-t0)*1000), "meta": {"session": new_sid}}
    except Exception as e:
        return {"ok": False, "text": f"[claude parse failed] {e}\n{err or out[:400]}",
                "ms": int((time.time()-t0)*1000), "meta": {}}


def _agy_cmd(text, sid, cfg):
    # agy is print-only (no JSON/streaming): one prompt in, plain text out
    cmd = [AGY_BIN, "--print-timeout", "600s"]
    cmd += ["--dangerously-skip-permissions"] if cfg["mode"] == "skip-permissions" else ["--sandbox"]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]
    if sid:
        cmd += ["--conversation", sid]
    cmd += ["-p", text]
    return cmd


def ask_antigravity(text):
    t0 = time.time()
    if not AGY_BIN:
        return {"ok": False, "text": "[agy CLI not found] Install the Antigravity CLI, or set DUO_AGY_BIN to its path",
                "ms": 0, "meta": {}}
    with LOCK:
        sid, cwd = STATE["antigravity"]["id"], STATE["antigravity"]["cwd"]
        cfg = dict(AGENT_CFG["antigravity"])
    rc, out, err = run(_agy_cmd(text, sid, cfg), cwd)
    final = (out or "").strip()
    # agy never prints the conversation id; recover it from the cache it writes per workspace
    new_sid = _agy_last_conversation(cwd) or sid
    if new_sid:
        with LOCK:
            STATE["antigravity"]["id"] = new_sid
    return {"ok": bool(final), "text": final or f"[agy no response]\n{err[:400]}",
            "ms": int((time.time()-t0)*1000), "meta": {"conversation": new_sid}}


def _fmt_tool(name, inp):
    inp = inp or {}
    if name == "Bash":
        return "⌘ " + str(inp.get("command", ""))[:120]
    if name in ("Edit", "Write", "NotebookEdit"):
        return "✎ " + str(inp.get("file_path", ""))
    if name == "Read":
        return "📖 " + str(inp.get("file_path", ""))
    if name in ("Grep", "Glob"):
        return "🔎 " + str(inp.get("pattern", ""))[:80]
    return "▸ " + str(name) + " " + json.dumps(inp)[:80]


def run_stream_claude(text, emit):
    if not CLAUDE_BIN:
        emit({"engine": "claude", "k": "text", "t": "[claude CLI not found]"}); return ""
    with LOCK:
        sid, cwd = STATE["claude"]["id"], STATE["claude"]["cwd"]
        cfg = dict(AGENT_CFG["claude"])
    cmd = [CLAUDE_BIN, "-p", text, "--output-format", "stream-json", "--verbose"]
    if sid:
        cmd += ["--resume", sid]
    cmd += ["--permission-mode", cfg["mode"] or "default"]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]
    if cfg["effort"]:
        cmd += ["--effort", cfg["effort"]]
    cmd += ["--settings", json.dumps({"fastMode": bool(cfg.get("fast"))})]
    final, newsid = "", None
    try:
        p = subprocess.Popen(cmd, cwd=cwd or HERE, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t == "assistant":
                for b in d.get("message", {}).get("content", []):
                    if b.get("type") == "text" and b.get("text"):
                        emit({"engine": "claude", "k": "text", "t": b["text"]})
                    elif b.get("type") == "tool_use":
                        emit({"engine": "claude", "k": "tool", "t": _fmt_tool(b.get("name"), b.get("input"))})
            elif t == "result":
                final = d.get("result", "") or final
                newsid = d.get("session_id")
        p.wait()
    except Exception as e:
        emit({"engine": "claude", "k": "text", "t": f"[error] {e}"})
    if newsid:
        with LOCK:
            STATE["claude"]["id"] = newsid
    return final


def run_stream_antigravity(text, emit):
    if not AGY_BIN:
        emit({"engine": "antigravity", "k": "text", "t": "[agy CLI not found]"}); return ""
    with LOCK:
        sid, cwd = STATE["antigravity"]["id"], STATE["antigravity"]["cwd"]
        cfg = dict(AGENT_CFG["antigravity"])
    # agy has no streaming/JSON event API, so there's no live step-by-step feed:
    # run the one-shot and emit the final answer in a single block.
    emit({"engine": "antigravity", "k": "tool", "t": "▸ agy running…"})
    final = ""
    try:
        p = subprocess.Popen(_agy_cmd(text, sid, cfg), cwd=cwd or HERE, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate()
        final = (out or "").strip()
        if not final and err:
            final = f"[agy error] {err.strip()[:400]}"
        if final:
            emit({"engine": "antigravity", "k": "text", "t": final})
    except Exception as e:
        emit({"engine": "antigravity", "k": "text", "t": f"[error] {e}"})
    new_sid = _agy_last_conversation(cwd) or sid
    if new_sid:
        with LOCK:
            STATE["antigravity"]["id"] = new_sid
    return final


def handle_stream(target, text, emit):
    with LOCK:
        proj = SETTINGS["project"]
        writable = {e: (AGENT_CFG[e]["mode"] in _WRITABLE) for e in AGENT_CFG}
    before = _snapshot(proj) if proj else {}
    engines = [e for e in ("claude", "antigravity") if target in (e, "both")]
    results = {}

    def work(e):
        t0 = time.time()
        fn = run_stream_claude if e == "claude" else run_stream_antigravity
        final = fn(text, emit)
        results[e] = {"text": final, "ms": int((time.time() - t0) * 1000)}
        emit({"engine": e, "k": "done", "ms": results[e]["ms"]})

    threads = [threading.Thread(target=work, args=(e,)) for e in engines]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    changed = _diff(before, _snapshot(proj)) if proj else []
    for e in engines:
        if results.get(e, {}).get("text"):
            record_behavior(e, results[e]["text"], proj, changed, writable.get(e, False))


def handle_send(target, text):
    out, threads = {}, []
    with LOCK:
        proj = SETTINGS["project"]
        writable = {e: (AGENT_CFG[e]["mode"] in _WRITABLE) for e in AGENT_CFG}
    before = _snapshot(proj) if proj else {}
    jobs = [j for j in (("claude", ask_claude), ("antigravity", ask_antigravity))
            if target in (j[0], "both")]
    for name, fn in jobs:
        def work(n=name, f=fn):
            out[n] = f(text)
        th = threading.Thread(target=work); th.start(); threads.append(th)
    for th in threads:
        th.join()
    changed = _diff(before, _snapshot(proj)) if proj else []
    for name, res in out.items():
        if res and res.get("ok"):
            record_behavior(name, res.get("text", ""), proj, changed, writable.get(name, False))
    return out


# ---------- list / read past sessions ----------
def _first_user_and_cwd_claude(path):
    cwd, first, title = None, "", ""
    try:
        for ln in open(path):
            d = json.loads(ln)
            cwd = cwd or d.get("cwd")
            if d.get("type") == "ai-title" and not title:
                title = d.get("aiTitle", "") or d.get("title", "") or ""
            if not first and d.get("type") == "user":
                c = d.get("message", {}).get("content")
                if isinstance(c, str):
                    first = c
                elif isinstance(c, list):
                    for b in c:
                        if b.get("type") == "text":
                            first = b.get("text", ""); break
            if cwd and first and title:
                break
    except Exception:
        pass
    return cwd, title[:80].replace("\n", " "), first[:80].replace("\n", " ")


def _agy_last_conversation(cwd):
    # agy doesn't print the conversation id; recover it from the cache it writes per workspace
    try:
        d = json.load(open(CFG["agy_last"]))
        return d.get(cwd) or d.get(os.path.realpath(cwd))
    except Exception:
        return None


def _agy_workspace_map():
    # conversation id -> workspace path, merged from the two cache files agy maintains
    m = {}
    for key in ("agy_last", "agy_projects"):
        try:
            for ws, cid in json.load(open(CFG[key])).items():
                if cid and ws:
                    m.setdefault(cid, ws)
        except Exception:
            pass
    return m


def _agy_history():
    # conversation id -> (first prompt = title, [all user prompts]); from history.jsonl
    titles, prompts = {}, {}
    try:
        for ln in open(CFG["agy_history"]):
            d = json.loads(ln)
            cid = d.get("conversationId")
            disp = (d.get("display") or "").replace("\n", " ").strip()
            if not cid or not disp:
                continue
            titles.setdefault(cid, disp[:80])
            prompts.setdefault(cid, []).append(disp)
    except Exception:
        pass
    return titles, prompts


def _proj_name(cwd, labels=None):
    if labels and cwd in labels:
        return labels[cwd]
    if cwd == HOME:
        return "ungrouped (home)"
    base = os.path.basename((cwd or "").rstrip("/"))
    return base or cwd or "(unknown)"


# ---- decode Chrome Local Storage leveldb (snappy + table format) to read Claude custom groups ----
import struct
_CG_CACHE = {"mtime": 0, "groups": {}, "assign": {}}
_CG_PERSIST = os.path.join(HERE, "duo_claude_groups.json")


def _uvarint(b, p):
    r = s = 0
    while True:
        c = b[p]; p += 1
        r |= (c & 0x7f) << s
        if not (c & 0x80):
            return r, p
        s += 7


def _snappy(data):
    p, ln, s = 0, 0, 0
    while True:
        c = data[p]; p += 1; ln |= (c & 0x7f) << s
        if not (c & 0x80):
            break
        s += 7
    out = bytearray()
    while p < len(data):
        tag = data[p]; p += 1; t = tag & 3
        if t == 0:
            v = tag >> 2
            if v < 60:
                l = v + 1
            else:
                nb = v - 59
                l = int.from_bytes(data[p:p + nb], "little") + 1; p += nb
            out += data[p:p + l]; p += l
        else:
            if t == 1:
                l = ((tag >> 2) & 7) + 4
                off = ((tag >> 5) << 8) | data[p]; p += 1
            elif t == 2:
                l = (tag >> 2) + 1
                off = int.from_bytes(data[p:p + 2], "little"); p += 2
            else:
                l = (tag >> 2) + 1
                off = int.from_bytes(data[p:p + 4], "little"); p += 4
            st = len(out) - off
            for i in range(l):
                out.append(out[st + i])
    return bytes(out)


def _ldb_blocks(b):
    blocks = []
    if len(b) < 48:
        return blocks
    foot = b[-48:]
    p = 0
    _o, p = _uvarint(foot, p); _s, p = _uvarint(foot, p)   # metaindex handle (unused)
    ioff, p = _uvarint(foot, p); isz, p = _uvarint(foot, p)  # index handle

    def rd(o, s):
        raw = b[o:o + s]
        return _snappy(raw) if b[o + s] == 1 else raw
    idx = rd(ioff, isz)
    num = struct.unpack("<I", idx[-4:])[0]
    end = len(idx) - 4 - num * 4
    p, prev = 0, b""
    while p < end:
        sh, p = _uvarint(idx, p); ns, p = _uvarint(idx, p); vl, p = _uvarint(idx, p)
        key = prev[:sh] + idx[p:p + ns]; p += ns
        val = idx[p:p + vl]; p += vl; prev = key
        o, q = _uvarint(val, 0); s, q = _uvarint(val, q)
        try:
            blocks.append(rd(o, s))
        except Exception:
            pass
    return blocks


def _scan_groups(text, groups, assign):
    for m in re.finditer(r'"id":"(cg-[a-f0-9-]+)","name":"([^"]{1,80})"', text):
        groups.setdefault(m.group(1), m.group(2))
    for m in re.finditer(r'"code:local_([a-f0-9-]+)":"(cg-[a-f0-9-]+)"', text):
        assign.setdefault(m.group(1), m.group(2))


def _claude_groups():
    ls = os.path.join(HOME, "Library", "Application Support", "Claude", "Local Storage", "leveldb")
    try:
        files = sorted(glob.glob(os.path.join(ls, "*")), key=os.path.getmtime, reverse=True)
    except Exception:
        files = []
    newest = os.path.getmtime(files[0]) if files else 0
    if _CG_CACHE["mtime"] == newest and _CG_CACHE["groups"]:
        return _CG_CACHE["groups"], _CG_CACHE["assign"]
    groups, assign = {}, {}
    for f in files:
        try:
            raw = open(f, "rb").read()
        except Exception:
            continue
        _scan_groups(raw.replace(b"\x00", b"").decode("latin-1", "ignore"), groups, assign)  # plaintext (.log)
        if f.endswith(".ldb"):
            try:
                for blk in _ldb_blocks(raw):  # decompress snappy blocks
                    _scan_groups(blk.replace(b"\x00", b"").decode("latin-1", "ignore"), groups, assign)
            except Exception:
                pass
    if groups:  # read ok -> persist, so we still have the last groups after leveldb compacts
        try:
            json.dump({"groups": groups, "assign": assign}, open(_CG_PERSIST, "w"))
        except Exception:
            pass
    else:  # can't read -> fall back to last good cache
        try:
            d = json.load(open(_CG_PERSIST))
            groups, assign = d.get("groups", {}), d.get("assign", {})
        except Exception:
            pass
    _CG_CACHE.update(mtime=newest, groups=groups, assign=assign)
    return groups, assign


def list_sessions(engine, limit=300, include_hidden=False):
    rows = []
    if engine == "claude":
        # prefer the Claude desktop app index (clean titles); fall back to scanning CLI projects jsonl
        if CLAUDE_SESS:
            groups, assign = _claude_groups()
            for f in glob.glob(os.path.join(CLAUDE_SESS, "**", "local_*.json"), recursive=True):
                try:
                    d = json.load(open(f))
                except Exception:
                    continue
                cid = d.get("cliSessionId")
                if not cid:
                    continue
                cwd = d.get("cwd") or d.get("originCwd") or HERE
                sid = (d.get("sessionId") or "").replace("local_", "")
                grp = groups.get(assign.get(sid))  # custom group name (if any)
                rows.append({"id": cid, "cwd": cwd, "project": grp or _proj_name(cwd),
                             "title": d.get("title", ""), "first": "",
                             "archived": bool(d.get("isArchived")),
                             "ts": int((d.get("lastActivityAt") or d.get("createdAt") or 0) / 1000)})
        if not rows:
            # fallback: CLI-only, no desktop index -> scan ~/.claude/projects jsonl (use aiTitle)
            for f in glob.glob(os.path.join(CLAUDE_PROJ, "*", "*.jsonl")):
                sid = os.path.basename(f)[:-6]
                cwd, title, first = _first_user_and_cwd_claude(f)
                cwd = cwd or HERE
                rows.append({"id": sid, "cwd": cwd, "project": _proj_name(cwd),
                             "title": title, "first": first,
                             "ts": int(os.path.getmtime(f))})
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return _apply_overrides("claude", rows[:limit], include_hidden)
    else:
        # agy stores each conversation as an opaque <id>.pb; titles/workspace come from the cache + history
        ws_map = _agy_workspace_map()
        titles, _ = _agy_history()
        files = glob.glob(os.path.join(AGY_CONVS, "*.pb"))
        files.sort(key=os.path.getmtime, reverse=True)
        for f in files[:limit]:
            sid = os.path.basename(f)[:-3]  # strip ".pb"
            cwd = ws_map.get(sid) or HERE
            rows.append({"id": sid, "cwd": cwd, "project": _proj_name(cwd),
                         "title": titles.get(sid, ""), "first": "",
                         "ts": int(os.path.getmtime(f))})
    return _apply_overrides("antigravity", rows, include_hidden)


def _find_file(engine, sid):
    # only Claude keeps readable jsonl transcripts on disk; agy transcripts are opaque protobuf
    hits = glob.glob(os.path.join(CLAUDE_PROJ, "*", sid + ".jsonl"))
    return hits[0] if hits else None


def read_transcript(engine, sid):
    if engine != "claude":
        # agy conversations are stored as opaque protobuf; only the user prompts are recoverable (history.jsonl)
        _, prompts = _agy_history()
        msgs = [{"role": "user", "text": p} for p in prompts.get(sid, [])]
        if msgs:
            msgs.append({"role": "bot",
                         "text": "[Antigravity replies aren't stored in a readable form — resume the session to continue it.]"})
        return msgs[-200:]
    f = _find_file(engine, sid)
    if not f:
        return []
    msgs = []
    for ln in open(f):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        t = d.get("type")
        if t in ("user", "assistant"):
            c = d.get("message", {}).get("content")
            txt = ""
            if isinstance(c, str):
                txt = c
            elif isinstance(c, list):
                txt = "".join(b.get("text", "") for b in c if b.get("type") == "text")
            if txt.strip():
                msgs.append({"role": "user" if t == "user" else "bot", "text": txt})
    return msgs[-200:]


# ---------- token usage panel (parse local jsonl, inspired by mercury-cache-panel) ----------
from datetime import datetime


def _iso_epoch(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def token_stats(window_sec=86400):
    # Only Claude writes per-turn usage to disk; agy keeps usage inside its protobuf store, so it has no card here.
    cutoff = time.time() - window_sec
    out = {"claude": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                      "cost": 0.0, "sessions": 0}}
    # Claude: sum each assistant message by its own timestamp (only within the window); input and cache_read are separate
    for f in glob.glob(os.path.join(CLAUDE_PROJ, "*", "*.jsonl")):
        try:
            if os.path.getmtime(f) < cutoff:
                continue
        except OSError:
            continue
        hit = False
        try:
            for ln in open(f):
                d = json.loads(ln)
                u = d.get("message", {}).get("usage")
                if not u:
                    continue
                ep = _iso_epoch(d.get("timestamp"))
                if ep is not None and ep < cutoff:
                    continue
                hit = True
                a = out["claude"]
                a["input"] += u.get("input_tokens", 0)
                a["output"] += u.get("output_tokens", 0)
                a["cache_read"] += u.get("cache_read_input_tokens", 0)
                a["cache_write"] += u.get("cache_creation_input_tokens", 0)
        except Exception:
            pass
        if hit:
            out["claude"]["sessions"] += 1
    a = out["claude"]
    p = PRICING["claude"]
    a["cost"] = round(a["input"] / 1e6 * p["input"] + a["output"] / 1e6 * p["output"]
                      + a["cache_read"] / 1e6 * p["cache_read"]
                      + a["cache_write"] / 1e6 * p["cache_write"], 2)
    denom = a["input"] + a["cache_read"] + a["cache_write"]
    a["cache_pct"] = round(100 * a["cache_read"] / denom, 1) if denom else 0.0
    a["total_tokens"] = a["input"] + a["output"] + a["cache_read"] + a["cache_write"]
    return out


# ---------- Antigravity quota panel: real /usage data scraped from the agy TUI ----------
# agy exposes NO usage numbers via --print, its protobuf store, or any subcommand. The only
# source of real usage is the interactive `/usage` slash command, which is intercepted only
# when typed into the live TUI. So we drive agy through a pseudo-terminal, type `/usage`,
# wait for the "Models & Quota" panel to render, and parse the per-group quota gauges.
# It reports % remaining + refresh windows (5-hour / weekly), NOT token counts or USD cost.
_AGY_USAGE_CACHE = {"ts": 0, "data": None}
_AGY_USAGE_TTL = 60  # the scrape takes ~10-15s, so serve a cached snapshot and refresh in the background
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB0]|"
    r"\x1b[=>PX^_].*?(?:\x1b\\|\x07)|[\x00-\x08\x0b\x0c\x0e-\x1f]")
_USAGE_GROUP = re.compile(r"^[A-Z][A-Z0-9 &/]*MODELS$")
_USAGE_PCT = re.compile(r"(\d+(?:\.\d+)?)%")
_USAGE_REMAIN = re.compile(r"(\d+)%\s*remaining")
_USAGE_REFRESH = re.compile(r"Refreshes in ([0-9hms ]+)")
_USAGE_MODELS = re.compile(r"Models within this group:\s*(.+)$")
_USAGE_ACCOUNT = re.compile(r"Account:\s*(\S+)")


def _strip_ansi(b):
    return _ANSI_RE.sub("", b)


def _parse_agy_usage(text):
    # The cumulative buffer holds several redraws (the panel re-renders as quota loads), so
    # isolate the LAST frame (everything after the final panel title) and parse only that.
    i = text.rfind("Models & Quota")
    if i >= 0:
        text = text[i:]
    groups, order = {}, []
    account = None
    cur_group = cur_limit = None
    for raw in text.splitlines():
        stripped = raw.strip()
        # the explanatory footer (│-prefixed) and help line repeat "weekly limit"/"5-hour
        # limit" as prose — stop before they corrupt the parse state
        if stripped.startswith("│") or stripped.startswith("↑/↓"):
            break
        line = stripped.lstrip("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣷⣯⣟⡿⢿⣻⣽└├─ ").strip()
        if not line:
            continue
        m = _USAGE_ACCOUNT.search(line)
        if m and account is None:
            account = m.group(1)
        if _USAGE_GROUP.match(line):
            cur_group, cur_limit = line, None
            if cur_group not in groups:
                groups[cur_group] = {"group": cur_group, "models": "",
                                     "weekly": None, "five_hour": None}
                order.append(cur_group)
            continue
        if cur_group is None:
            continue
        mm = _USAGE_MODELS.search(line)
        if mm:
            groups[cur_group]["models"] = mm.group(1).strip()
            continue
        low = line.lower()
        if "weekly limit" in low:
            cur_limit = "weekly"; continue
        if "five hour limit" in low or "5-hour limit" in low or "5 hour limit" in low:
            cur_limit = "five_hour"; continue
        if cur_limit is None:
            continue
        # data lines belonging to the current group+limit: a bar with a %, a "X% remaining
        # · Refreshes in ..." line, or "Quota available"
        cell = groups[cur_group].get(cur_limit) or {
            "pct": None, "remaining_pct": None, "refresh": None, "available": False}
        touched = False
        if "quota available" in low:
            cell["available"] = True
            if cell["pct"] is None:
                cell["pct"] = 100.0
            touched = True
        if line.startswith("["):  # the gauge bar carries the precise % remaining
            pm = _USAGE_PCT.search(line)
            if pm:
                cell["pct"] = float(pm.group(1)); touched = True
        rm = _USAGE_REMAIN.search(line)
        if rm:
            cell["remaining_pct"] = int(rm.group(1)); touched = True
        rf = _USAGE_REFRESH.search(line)
        if rf:
            cell["refresh"] = rf.group(1).strip(); touched = True
        if touched:
            groups[cur_group][cur_limit] = cell
    out = [groups[g] for g in order
           if groups[g]["weekly"] or groups[g]["five_hour"]]
    return account, out


def _agy_usage_scrape(timeout=28):
    # Drive `agy` through a PTY, type /usage, wait for the quota panel, return parsed data.
    # Unix-only (pty/termios/fcntl); on any failure we return None so the card degrades to n/a.
    if not AGY_BIN:
        return None
    try:
        import pty, select, termios, fcntl
    except Exception:
        return None
    pid, fd = pty.fork()
    if pid == 0:  # child: become the agy TUI
        try:
            os.chdir(HERE)
            os.environ["TERM"] = "xterm-256color"
            os.execvp(AGY_BIN, [AGY_BIN])
        except Exception:
            os._exit(127)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    except Exception:
        pass
    buf = b""
    start = time.time()
    last_data = start
    typed = False
    seen_panel = False
    settle_until = None
    try:
        while True:
            now = time.time()
            if now - start > timeout:
                break
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                last_data = now
            # type /usage once the TUI has rendered and gone quiet
            if not typed and (now - start) > 5 and (now - last_data) > 1.0:
                try:
                    os.write(fd, b"/usage")
                    time.sleep(0.7)
                    os.write(fd, b"\r")
                except OSError:
                    break
                typed = True
            # once the quota panel has loaded, read a little longer then stop
            if typed and not seen_panel:
                txt = _strip_ansi(buf.decode("utf-8", "replace"))
                # "Refreshes in"/"Quota available" only appear after the quota summary loads
                if "Refreshes in" in txt or "Quota available" in txt:
                    seen_panel = True
                    settle_until = now + 2.0
            if seen_panel and settle_until and now >= settle_until:
                break
    finally:
        try:
            os.kill(pid, 9)
        except Exception:
            pass
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
    text = _strip_ansi(buf.decode("utf-8", "replace"))
    account, groups = _parse_agy_usage(text)
    if not groups:
        return {"available": False,
                "reason": "no quota panel (agy not logged in, or TUI changed)"}
    return {"available": True, "account": account, "groups": groups,
            "fetched_at": int(time.time())}


def agy_usage(force=False):
    # Serve the cached snapshot (kept fresh by the background refresher). Only the very first
    # call before the background thread has populated the cache scrapes synchronously.
    with LOCK:
        c = dict(_AGY_USAGE_CACHE)
    if c["data"] is not None and not force:
        return c["data"]
    data = _agy_usage_scrape() or {"available": False, "reason": "unavailable"}
    with LOCK:
        _AGY_USAGE_CACHE.update(ts=time.time(), data=data)
    return data


def _agy_usage_loop():
    # Background refresher: repopulate the cache every TTL seconds without blocking requests.
    while True:
        try:
            data = _agy_usage_scrape()
            if data is not None:
                with LOCK:
                    _AGY_USAGE_CACHE.update(ts=time.time(), data=data)
        except Exception:
            pass
        time.sleep(_AGY_USAGE_TTL)


# ---------- watchdog: did the AI actually change the disk, or just talk? ----------
_BACKTICK = re.compile(r"`([^`\n]{1,120}?)`")
_BACKTICK = re.compile(r"`([^`\n]{1,120}?)`")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
# claim verbs ('I did X') in English + Chinese
_CLAIM = re.compile(
    r"(建立|新增|建好|寫入|寫好|修改|改好|更新|刪除|刪掉|執行|跑了|跑完|測試過|部署|安裝|"
    r"已完成|完成了|做好了|搞定|加上了|加好|實作|implemented|created|added|wrote|updated|"
    r"modified|deleted|ran\b|executed|tested|deployed|installed|fixed|done\b|finished|set up)",
    re.I)
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next",
              "dist", "build", ".cache", "target"}


def _snapshot(cwd):
    snap = {}
    if not cwd or not os.path.isdir(cwd):
        return snap
    n = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            p = os.path.join(root, f)
            try:
                snap[p] = (os.path.getmtime(p), os.path.getsize(p))
            except OSError:
                pass
            n += 1
            if n > 30000:
                return snap
    return snap


def _diff(before, after):
    return [p for p, v in after.items() if before.get(p) != v]  # created or modified


def check_honesty(engine, text, cwd, changed, write):
    text = text or ""
    actions = len(_CLAIM.findall(text))
    changed = changed or []
    changed_names = set()
    for p in changed:
        changed_names.add(os.path.basename(p))
        try:
            changed_names.add(os.path.relpath(p, cwd))
        except Exception:
            pass
    claims, seen = [], set()
    for tok in _BACKTICK.findall(text):
        tok = tok.strip()
        if not tok or " " in tok or tok in seen:
            continue
        if not (_EXT.search(tok) or "/" in tok):
            continue
        seen.add(tok)
        p = tok if os.path.isabs(tok) else os.path.join(cwd or HERE, tok)
        st = "missing"
        try:
            if os.path.isfile(p):
                sz = os.path.getsize(p)
                age = time.time() - os.path.getmtime(p)
                if sz == 0:
                    st = "empty"
                elif tok in changed_names or os.path.basename(tok) in changed_names or age < 300:
                    st = "verified"   # actually changed this turn
                else:
                    st = "exists"     # exists but not touched this turn (just mentioned)
        except Exception:
            pass
        claims.append({"path": tok, "status": st})
        if len(claims) >= 12:
            break
    bad = [c for c in claims if c["status"] in ("missing", "empty")]
    verified = [c for c in claims if c["status"] == "verified"]
    # the core call-out: in a writable mode, many claimed actions but 0 disk change and no verified file = busywork
    bluff = write and actions >= 2 and not changed and not verified
    if bad:
        verdict, reason = "warn", "claimed files have no evidence (missing/empty)"
    elif bluff:
        verdict, reason = "warn", f"{actions} actions claimed, 0 files changed on disk (looks like busywork)"
    elif actions or claims or changed:
        verdict, reason = "ok", f"{len(changed)} files changed · {len(verified)} claims verified"
    else:
        verdict, reason = "none", ""
    return {"ts": int(time.time()), "engine": engine, "claims": claims,
            "actions": actions, "changed": len(changed),
            "bad": len(bad), "verdict": verdict, "reason": reason}


def record_behavior(engine, text, cwd, changed, write):
    rec = check_honesty(engine, text, cwd, changed, write)
    # looping: accumulate consecutive 'claimed actions but 0 change'; reset on real change or a no-claim turn
    if write:
        if rec["actions"] >= 1 and rec["changed"] == 0:
            STREAK[engine] = STREAK.get(engine, 0) + 1
        else:
            STREAK[engine] = 0
    rec["streak"] = STREAK.get(engine, 0)
    rec["circling"] = rec["streak"] >= 3
    if rec["circling"]:
        rec["verdict"] = "warn"
        rec["reason"] = f"{rec['streak']} turns in a row claiming progress with 0 disk changes (looping)"
    if rec["verdict"] == "none":
        return
    with LOCK:
        BEHAVIOR.insert(0, rec)
        del BEHAVIOR[30:]


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif u.path == "/logo.svg":
            try:
                with open(os.path.join(HERE, "logo.svg"), "rb") as f:
                    self._send(200, f.read(), "image/svg+xml")
            except Exception:
                self._send(404, "no logo", "text/plain")
        elif u.path == "/api/sessions":
            eng = q.get("engine", ["claude"])[0]
            hidden = q.get("hidden", ["0"])[0] == "1"
            self._send(200, json.dumps(list_sessions(eng, include_hidden=hidden)))
        elif u.path == "/api/transcript":
            eng = q.get("engine", ["claude"])[0]
            sid = q.get("id", [""])[0]
            self._send(200, json.dumps(read_transcript(eng, sid)))
        elif u.path == "/api/state":
            self._send(200, json.dumps(STATE))
        elif u.path == "/api/project":
            self._send(200, json.dumps({
                "project": SETTINGS["project"],
                "claude_cwd": STATE["claude"]["cwd"], "antigravity_cwd": STATE["antigravity"]["cwd"]}))
        elif u.path == "/api/agent-config":
            self._send(200, json.dumps(AGENT_CFG))
        elif u.path == "/api/projects":
            # attachable projects: the shared project + distinct cwds seen across sessions
            seen, out = set(), []
            if SETTINGS["project"]:
                out.append({"path": SETTINGS["project"], "label": os.path.basename(SETTINGS["project"]) + " (shared)"})
                seen.add(SETTINGS["project"])
            for eng in ("claude", "antigravity"):
                for r in list_sessions(eng, limit=300):
                    c = r.get("cwd")
                    if c and c not in seen and os.path.isdir(c):
                        seen.add(c)
                        out.append({"path": c, "label": r.get("project") or os.path.basename(c)})
            self._send(200, json.dumps(out[:40]))
        elif u.path == "/api/files":
            base = SETTINGS["project"] or HERE
            rel = q.get("dir", [""])[0]
            d = os.path.normpath(os.path.join(base, rel))
            if not d.startswith(os.path.normpath(base)):  # don't escape the project directory
                d, rel = base, ""
            entries = []
            try:
                for name in sorted(os.listdir(d), key=lambda x: (not os.path.isdir(os.path.join(d, x)), x.lower())):
                    if name.startswith("."):
                        continue
                    entries.append({"name": name, "dir": os.path.isdir(os.path.join(d, name))})
            except Exception:
                pass
            self._send(200, json.dumps({"base": base, "rel": os.path.relpath(d, base) if d != base else "",
                                        "entries": entries[:400]}))
        elif u.path == "/api/tokens":
            now = time.time()
            if _TOK_CACHE["data"] and now - _TOK_CACHE["ts"] < 10:
                self._send(200, json.dumps(_TOK_CACHE["data"]))
            else:
                d = token_stats()
                _TOK_CACHE.update(ts=now, data=d)
                self._send(200, json.dumps(d))
        elif u.path == "/api/agy-usage":
            self._send(200, json.dumps(agy_usage()))
        elif u.path == "/api/behavior":
            self._send(200, json.dumps({"records": BEHAVIOR, "streak": STREAK}))
        elif u.path == "/api/engines":
            self._send(200, json.dumps({
                "claude": {"available": bool(CLAUDE_BIN), "bin": CLAUDE_BIN,
                           "title_source": "desktop" if CLAUDE_SESS else "cli-jsonl"},
                "antigravity": {"available": bool(AGY_BIN), "bin": AGY_BIN},
            }))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        if self.path == "/api/upload":
            if n > 60 * 1024 * 1024:
                self._send(413, json.dumps({"error": "file too large (>60MB)"})); return
            fname = os.path.basename(unquote(self.headers.get("X-Filename", "file"))) or "file"
            fname = fname.replace("\x00", "")
            base = SETTINGS["project"] or HERE
            updir = os.path.join(base, ".duo_uploads")
            try:
                os.makedirs(updir, exist_ok=True)
                stem, ext = os.path.splitext(fname)
                dest, i = os.path.join(updir, fname), 1
                while os.path.exists(dest):
                    dest = os.path.join(updir, f"{stem}_{i}{ext}"); i += 1
                with open(dest, "wb") as f:
                    f.write(body)
                self._send(200, json.dumps({"ok": True, "rel": os.path.relpath(dest, base),
                                            "name": os.path.basename(dest)}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/stream":
            req = json.loads(body or b"{}")
            target = req.get("target", "both")
            text = (req.get("text") or "").strip()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            wlock = threading.Lock()

            def emit(ev):
                try:
                    with wlock:
                        self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
                        self.wfile.flush()
                except Exception:
                    pass
            try:
                if text:
                    handle_stream(target, text, emit)
            except Exception as e:
                emit({"k": "error", "t": str(e)})
            return
        req = json.loads(body or b"{}")
        if self.path == "/api/send":
            text = (req.get("text") or "").strip()
            if not text:
                self._send(400, json.dumps({"error": "empty"})); return
            try:
                self._send(200, json.dumps(handle_send(req.get("target", "both"), text)))
            except subprocess.TimeoutExpired:
                self._send(200, json.dumps({"error": "timeout"}))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}))
        elif self.path == "/api/load":
            eng = req.get("engine"); sid = req.get("id"); cwd = req.get("cwd") or HERE
            if eng in STATE:
                with LOCK:
                    STATE[eng]["id"] = sid; STATE[eng]["cwd"] = cwd
                self._send(200, json.dumps({"ok": True, "transcript": read_transcript(eng, sid)}))
            else:
                self._send(400, json.dumps({"error": "bad engine"}))
        elif self.path == "/api/session/patch":
            eng = req.get("engine"); sid = req.get("id"); patch = req.get("patch", {})
            if eng not in ("claude", "antigravity") or not sid:
                self._send(400, json.dumps({"error": "bad args"})); return
            with LOCK:
                ov = _load_overrides()
                ov.setdefault(eng, {}).setdefault(sid, {})
                for k, v in patch.items():
                    if v is None:
                        ov[eng][sid].pop(k, None)
                    else:
                        ov[eng][sid][k] = v
                if not ov[eng][sid]:
                    ov[eng].pop(sid, None)
                _save_overrides(ov)
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/api/project":
            path = (req.get("path") or "").strip()
            if path and not os.path.isdir(os.path.expanduser(path)):
                self._send(400, json.dumps({"error": f"directory not found: {path}"})); return
            path = os.path.expanduser(path) if path else None
            with LOCK:
                changed = path is not None and path != SETTINGS["project"]
                SETTINGS["project"] = path
                if changed:  # only a project change restarts both agents in the new directory
                    for e in STATE:
                        STATE[e]["cwd"] = path
                        STATE[e]["id"] = None
            self._send(200, json.dumps({"ok": True, "project": path}))
        elif self.path == "/api/agent-config":
            eng = req.get("engine")
            if eng not in AGENT_CFG:
                self._send(400, json.dumps({"error": "bad engine"})); return
            with LOCK:
                for k in ("model", "mode", "effort"):
                    if k in req and k in AGENT_CFG[eng]:
                        if k == "mode":
                            AGENT_CFG[eng][k] = req[k] or ("default" if eng == "claude" else "sandbox")
                        else:
                            AGENT_CFG[eng][k] = req[k] or ""
                if "fast" in req and "fast" in AGENT_CFG[eng]:
                    AGENT_CFG[eng]["fast"] = bool(req["fast"])
            self._send(200, json.dumps({"ok": True, "cfg": AGENT_CFG[eng]}))
        elif self.path == "/api/clear-context":
            # clear cache = reset this agent's session; the next message starts fresh,
            # no longer re-caching the accumulated context (equivalent to /clear in Claude Code)
            eng = req.get("engine")
            engines = ["claude", "antigravity"] if eng in (None, "both") else [eng]
            with LOCK:
                for e in engines:
                    if e in STATE:
                        STATE[e]["id"] = None
                        STREAK[e] = 0
            self._send(200, json.dumps({"ok": True, "cleared": engines}))
        elif self.path == "/api/agent-cwd":
            eng = req.get("engine")
            cwd = (req.get("cwd") or "").strip()
            if eng not in STATE:
                self._send(400, json.dumps({"error": "bad engine"})); return
            if cwd and not os.path.isdir(os.path.expanduser(cwd)):
                self._send(400, json.dumps({"error": f"directory not found: {cwd}"})); return
            cwd = os.path.expanduser(cwd) if cwd else (SETTINGS["project"] or HERE)
            with LOCK:
                STATE[eng]["cwd"] = cwd
                STATE[eng]["id"] = None
                STREAK[eng] = 0
            self._send(200, json.dumps({"ok": True, "cwd": cwd}))
        elif self.path == "/api/reset":
            with LOCK:
                for e in STATE:
                    STATE[e]["id"] = None; STATE[e]["cwd"] = HERE; STREAK[e] = 0
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    port = int(os.environ.get("DUO_PORT", "8765"))
    banner = [
        f"Code Duo running  ->  http://localhost:{port}",
        f"  platform : {SYS}",
        f"  Claude CLI : {CLAUDE_BIN or 'not found (set DUO_CLAUDE_BIN or install Claude Code)'}",
        f"  Claude titles : {'desktop app index' if CLAUDE_SESS else 'CLI projects jsonl (aiTitle)'}",
        f"  Antigravity CLI : {AGY_BIN or 'not found (set DUO_AGY_BIN or install the Antigravity CLI)'}",
        f"  Antigravity home : {CFG['agy_home']}",
    ]
    print("\n".join(banner), flush=True)
    # keep the Antigravity /usage quota panel warm in the background (scrape is ~10-15s)
    if AGY_BIN:
        threading.Thread(target=_agy_usage_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
