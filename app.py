#!/usr/bin/env python3
# duo — 同一個介面，同時載入 Claude 與 Codex 兩個 agent，並能接續過去的對話。
# 不用 API：背後驅動 claude / codex 兩個 CLI，走訂閱登入。
import json, subprocess, threading, time, os, glob, re, shutil, platform
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
SYS = platform.system()


# ---------- 自動偵測 CLI 與 session 存放位置(跨平台 + 環境變數可覆寫) ----------
def _first_dir(paths):
    for p in paths:
        if p and os.path.isdir(p):
            return p
    return None


def _which(name, extra):
    return shutil.which(name) or next((p for p in extra if p and os.path.exists(p)), None)


def discover():
    # CLI 執行檔
    claude_bin = os.environ.get("DUO_CLAUDE_BIN") or _which("claude", [
        "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
        os.path.join(HOME, ".local/bin/claude"), os.path.join(HOME, ".npm-global/bin/claude"),
        os.path.join(HOME, ".bun/bin/claude")])
    codex_bin = os.environ.get("DUO_CODEX_BIN") or _which("codex", [
        "/Applications/Codex.app/Contents/Resources/codex",
        "/opt/homebrew/bin/codex", "/usr/local/bin/codex",
        os.path.join(HOME, ".local/bin/codex")])

    # Claude CLI 設定目錄(可被 CLAUDE_CONFIG_DIR 覆寫) -> projects/
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    claude_proj = os.path.join(cfg, "projects")

    # Claude 桌面 App 的 session 索引(有乾淨標題；非必須，沒裝桌面版就 None -> 改讀 jsonl)
    if SYS == "Darwin":
        sess_cands = [os.path.join(HOME, "Library/Application Support/Claude/claude-code-sessions")]
    elif SYS == "Windows":
        sess_cands = [os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code-sessions")]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
        sess_cands = [os.path.join(xdg, "Claude", "claude-code-sessions")]
    claude_sess = _first_dir(sess_cands)

    # Codex 家目錄(可被 CODEX_HOME 覆寫)
    cx = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    codex_dirs = [os.path.join(cx, "sessions"), os.path.join(cx, "archived_sessions")]

    return {
        "claude_bin": claude_bin, "codex_bin": codex_bin,
        "claude_proj": claude_proj, "claude_sess": claude_sess,
        "codex_home": cx, "codex_dirs": codex_dirs,
        "codex_index": os.path.join(cx, "session_index.jsonl"),
        "codex_state": os.path.join(cx, ".codex-global-state.json"),
    }


CFG = discover()
CLAUDE_BIN = CFG["claude_bin"]
CODEX_BIN = CFG["codex_bin"]
CLAUDE_PROJ = CFG["claude_proj"]
CLAUDE_SESS = CFG["claude_sess"]
CODEX_DIRS = CFG["codex_dirs"]

# 每個 agent 記住自己「正在接哪個對話」+「該從哪個目錄跑」(resume 綁 cwd)
STATE = {"claude": {"id": None, "cwd": HERE}, "codex": {"id": None, "cwd": HERE}}
LOCK = threading.Lock()

# duo 自己這層的覆寫(rename/pin/archive/delete)，不碰官方 App 資料
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


# ---------- 驅動兩顆引擎 ----------
def ask_claude(text):
    t0 = time.time()
    if not CLAUDE_BIN:
        return {"ok": False, "text": "[找不到 claude CLI] 請先安裝 Claude Code，或設 DUO_CLAUDE_BIN 指定路徑",
                "ms": 0, "meta": {}}
    with LOCK:
        sid, cwd = STATE["claude"]["id"], STATE["claude"]["cwd"]
    cmd = [CLAUDE_BIN, "-p", text, "--output-format", "json"]
    if sid:
        cmd += ["--resume", sid]
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
        return {"ok": False, "text": f"[claude 解析失敗] {e}\n{err or out[:400]}",
                "ms": int((time.time()-t0)*1000), "meta": {}}


def ask_codex(text):
    t0 = time.time()
    if not CODEX_BIN:
        return {"ok": False, "text": "[找不到 codex CLI] 請先安裝 Codex，或設 DUO_CODEX_BIN 指定路徑",
                "ms": 0, "meta": {}}
    with LOCK:
        tid, cwd = STATE["codex"]["id"], STATE["codex"]["cwd"]
    flags = ["--json", "--skip-git-repo-check", "-c", "sandbox_mode=read-only"]
    if tid:
        cmd = [CODEX_BIN, "exec", "resume", tid] + flags + [text]
    else:
        cmd = [CODEX_BIN, "exec"] + flags + [text]
    rc, out, err = run(cmd, cwd)
    msg, new_tid = "", None
    for ln in out.splitlines():
        try:
            d = json.loads(ln.strip())
        except Exception:
            continue
        if d.get("type") == "thread.started":
            new_tid = d.get("thread_id")
        it = d.get("item", {})
        if isinstance(it, dict) and it.get("type") == "agent_message":
            msg = it.get("text", msg)
    if new_tid:
        with LOCK:
            STATE["codex"]["id"] = new_tid
    return {"ok": bool(msg), "text": msg or f"[codex 無回應]\n{err[:400]}",
            "ms": int((time.time()-t0)*1000), "meta": {"thread": new_tid or tid}}


def handle_send(target, text):
    out, threads = {}, []
    jobs = [j for j in (("claude", ask_claude), ("codex", ask_codex))
            if target in (j[0], "both")]
    for name, fn in jobs:
        def work(n=name, f=fn):
            out[n] = f(text)
        th = threading.Thread(target=work); th.start(); threads.append(th)
    for th in threads:
        th.join()
    return out


# ---------- 列出 / 讀取過去的 session ----------
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


def _first_user_and_cwd_codex(path):
    cwd, first = None, ""
    try:
        for ln in open(path):
            d = json.loads(ln)
            t = d.get("type")
            pl = d.get("payload", {}) if isinstance(d.get("payload"), dict) else {}
            if t == "session_meta":
                cwd = cwd or pl.get("cwd")
            if t == "event_msg" and pl.get("type") == "user_message" and not first:
                first = pl.get("message", "")
            if cwd and first:
                break
    except Exception:
        pass
    return cwd, first[:80].replace("\n", " ")


def _codex_project_labels():
    # Codex 的「cwd 路徑 -> 專案顯示名」對照(跟 Codex App UI 一致)
    p = CFG["codex_state"]
    try:
        d = json.load(open(p))
        return d.get("electron-workspace-root-labels", {}) or {}
    except Exception:
        return {}


def _proj_name(cwd, labels=None):
    if labels and cwd in labels:
        return labels[cwd]
    if cwd == HOME:
        return "ungrouped（家目錄）"
    base = os.path.basename((cwd or "").rstrip("/"))
    return base or cwd or "(未知)"


_CX_PARSE_CACHE = {}  # path -> (mtime, (cwd, first))，避免高頻輪詢時重複讀沒變動的檔


def _codex_meta_cached(path):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return (None, "")
    hit = _CX_PARSE_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    res = _first_user_and_cwd_codex(path)
    _CX_PARSE_CACHE[path] = (mt, res)
    return res


def _codex_title_map():
    # Codex 官方標題索引：id -> thread_name（檔案時序排列，後者較新，last-wins）
    m = {}
    p = CFG["codex_index"]
    try:
        for ln in open(p):
            d = json.loads(ln)
            i, nm = d.get("id"), d.get("thread_name")
            if i and nm:
                m[i] = nm
    except Exception:
        pass
    return m


def list_sessions(engine, limit=300, include_hidden=False):
    rows = []
    if engine == "claude":
        # 優先讀 Claude 桌面 App 的官方索引(乾淨標題)；沒裝桌面版就降級掃 CLI 的 projects jsonl
        if CLAUDE_SESS:
            for f in glob.glob(os.path.join(CLAUDE_SESS, "**", "local_*.json"), recursive=True):
                try:
                    d = json.load(open(f))
                except Exception:
                    continue
                cid = d.get("cliSessionId")
                if not cid:
                    continue
                cwd = d.get("cwd") or d.get("originCwd") or HERE
                rows.append({"id": cid, "cwd": cwd, "project": _proj_name(cwd),
                             "title": d.get("title", ""), "first": "",
                             "archived": bool(d.get("isArchived")),
                             "ts": int((d.get("lastActivityAt") or d.get("createdAt") or 0) / 1000)})
        if not rows:
            # 降級：只用 CLI、沒桌面索引時，直接掃 ~/.claude/projects 的 jsonl(用 aiTitle)
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
        titles = _codex_title_map()
        labels = _codex_project_labels()
        files = []
        for dd in CODEX_DIRS:
            files += glob.glob(os.path.join(dd, "**", "*.jsonl"), recursive=True)
        files.sort(key=os.path.getmtime, reverse=True)
        for f in files[:limit]:
            cwd, first = _codex_meta_cached(f)
            cwd = cwd or HERE
            m = UUID_RE.search(os.path.basename(f))
            sid = m.group(0) if m else None
            if sid:
                rows.append({"id": sid, "cwd": cwd, "project": _proj_name(cwd, labels),
                             "title": titles.get(sid, ""), "first": first,
                             "ts": int(os.path.getmtime(f))})
    return _apply_overrides("codex", rows, include_hidden)


def _find_file(engine, sid):
    if engine == "claude":
        hits = glob.glob(os.path.join(CLAUDE_PROJ, "*", sid + ".jsonl"))
    else:
        hits = []
        for d in CODEX_DIRS:
            hits += glob.glob(os.path.join(d, "**", f"*{sid}*.jsonl"), recursive=True)
    return hits[0] if hits else None


def read_transcript(engine, sid):
    f = _find_file(engine, sid)
    if not f:
        return []
    msgs = []
    for ln in open(f):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if engine == "claude":
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
        else:
            pl = d.get("payload", {}) if isinstance(d.get("payload"), dict) else {}
            if d.get("type") == "event_msg":
                if pl.get("type") == "user_message":
                    msgs.append({"role": "user", "text": pl.get("message", "")})
                elif pl.get("type") == "agent_message":
                    msgs.append({"role": "bot", "text": pl.get("message", "")})
    return msgs[-200:]


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
        elif u.path == "/api/engines":
            self._send(200, json.dumps({
                "claude": {"available": bool(CLAUDE_BIN), "bin": CLAUDE_BIN,
                           "title_source": "desktop" if CLAUDE_SESS else "cli-jsonl"},
                "codex": {"available": bool(CODEX_BIN), "bin": CODEX_BIN},
            }))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
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
            if eng not in ("claude", "codex") or not sid:
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
        elif self.path == "/api/reset":
            with LOCK:
                for e in STATE:
                    STATE[e]["id"] = None; STATE[e]["cwd"] = HERE
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    port = int(os.environ.get("DUO_PORT", "8765"))
    banner = [
        f"Code Duo 跑起來了  ->  http://localhost:{port}",
        f"  平台: {SYS}",
        f"  Claude CLI : {CLAUDE_BIN or '✗ 找不到(設 DUO_CLAUDE_BIN 或裝 Claude Code)'}",
        f"  Claude 標題: {'桌面 App 索引' if CLAUDE_SESS else 'CLI projects jsonl(aiTitle)'}",
        f"  Codex CLI  : {CODEX_BIN or '✗ 找不到(設 DUO_CODEX_BIN 或裝 Codex)'}",
        f"  Codex home : {CFG['codex_home']}",
    ]
    print("\n".join(banner), flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
