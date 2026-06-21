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
# 共用協作設定:兩個 agent 一起做的真實專案目錄
SETTINGS = {"project": None}
# 每個 agent 各自的 model / mode / effort(像 Claude Code 底部那排控制)
AGENT_CFG = {
    "claude": {"model": "", "mode": "default", "effort": ""},
    "codex": {"model": "", "mode": "read-only", "effort": ""},
}
# 哪些 mode 代表「可以改檔」(給照妖鏡判斷是否該比對磁碟變動)
_WRITABLE = {"acceptEdits", "bypassPermissions", "auto", "dontAsk", "workspace-write", "danger-full-access"}
LOCK = threading.Lock()

# 每百萬 token 美金定價(來自 mercury-cache-panel)
PRICING = {
    "claude": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "codex":  {"input": 2.50, "output": 10.00, "cache_read": 0.25, "cache_write": 0.0},
}
# 照妖鏡:近期 agent 宣稱 vs 實證 的檢查結果(最新在前)
BEHAVIOR = []
# 兜圈子偵測:每個 agent「連續宣稱動作卻 0 變動」的輪數
STREAK = {"claude": 0, "codex": 0}
_TOK_CACHE = {"ts": 0, "data": None}

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
        cfg = dict(AGENT_CFG["claude"])
    cmd = [CLAUDE_BIN, "-p", text, "--output-format", "json"]
    if sid:
        cmd += ["--resume", sid]
    cmd += ["--permission-mode", cfg["mode"] or "default"]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]
    if cfg["effort"]:
        cmd += ["--effort", cfg["effort"]]
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
        cfg = dict(AGENT_CFG["codex"])
    mode = cfg["mode"] or "read-only"
    flags = ["--json", "--skip-git-repo-check", "-c", f"sandbox_mode={mode}"]
    if mode in _WRITABLE:
        flags += ["-c", "approval_policy=never"]
    if cfg["model"]:
        flags += ["-m", cfg["model"]]
    if cfg["effort"]:
        flags += ["-c", f"model_reasoning_effort={cfg['effort']}"]
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
    with LOCK:
        proj = SETTINGS["project"]
        writable = {e: (AGENT_CFG[e]["mode"] in _WRITABLE) for e in AGENT_CFG}
    before = _snapshot(proj) if proj else {}
    jobs = [j for j in (("claude", ask_claude), ("codex", ask_codex))
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


# ---------- Token 用量面板(解析本機 jsonl,參考 mercury-cache-panel) ----------
from datetime import datetime


def _iso_epoch(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def token_stats(window_sec=86400):
    cutoff = time.time() - window_sec
    out = {v: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
               "cost": 0.0, "sessions": 0} for v in ("claude", "codex")}
    # Claude:每則 assistant 訊息按自己的時間戳累加(只算近窗內);input 與 cache_read 是分開的
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
    # Codex:token_count 是「累計」,取窗內最後一筆減去窗前最後一筆得到近窗增量
    for dd in CODEX_DIRS:
        for f in glob.glob(os.path.join(dd, "**", "*.jsonl"), recursive=True):
            try:
                if os.path.getmtime(f) < cutoff:
                    continue
            except OSError:
                continue
            base, latest = None, None
            try:
                for ln in open(f):
                    d = json.loads(ln)
                    pl = d.get("payload", {}) if isinstance(d.get("payload"), dict) else {}
                    if pl.get("type") != "token_count":
                        continue
                    tu = pl.get("info", {}).get("total_token_usage")
                    if not tu:
                        continue
                    ep = _iso_epoch(d.get("timestamp"))
                    if ep is not None and ep < cutoff:
                        base = tu
                    else:
                        latest = tu
            except Exception:
                pass
            if latest:
                b = base or {}
                a = out["codex"]
                a["input"] += max(latest.get("input_tokens", 0) - b.get("input_tokens", 0), 0)
                a["output"] += max(latest.get("output_tokens", 0) - b.get("output_tokens", 0), 0)
                a["cache_read"] += max(latest.get("cached_input_tokens", 0) - b.get("cached_input_tokens", 0), 0)
                a["sessions"] += 1
    for v in ("claude", "codex"):
        a = out[v]
        p = PRICING[v]
        # Claude:input 不含 cache_read;Codex:input 含 cache_read 要扣掉
        billable_in = a["input"] - a["cache_read"] if v == "codex" else a["input"]
        billable_in = max(billable_in, 0)
        a["cost"] = round(billable_in / 1e6 * p["input"] + a["output"] / 1e6 * p["output"]
                          + a["cache_read"] / 1e6 * p["cache_read"]
                          + a["cache_write"] / 1e6 * p["cache_write"], 2)
        denom = billable_in + a["cache_read"] + a["cache_write"]
        a["cache_pct"] = round(100 * a["cache_read"] / denom, 1) if denom else 0.0
        a["total_tokens"] = billable_in + a["output"] + a["cache_read"] + a["cache_write"]
    return out


# ---------- 照妖鏡:AI 嘴上說做了一堆,實際磁碟有沒有動 ----------
_BACKTICK = re.compile(r"`([^`\n]{1,120}?)`")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
# 「我做了動作」的宣稱語言(中英)
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
    return [p for p, v in after.items() if before.get(p) != v]  # 新建或修改


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
                    st = "verified"   # 這一輪真的動過
                else:
                    st = "exists"     # 存在但這輪沒動(只是被提到)
        except Exception:
            pass
        claims.append({"path": tok, "status": st})
        if len(claims) >= 12:
            break
    bad = [c for c in claims if c["status"] in ("missing", "empty")]
    verified = [c for c in claims if c["status"] == "verified"]
    # 核心打臉:可改檔模式下,嘴上一堆動作但磁碟 0 變動且沒任何宣稱檔被動過 = 裝忙兜圈子
    bluff = write and actions >= 2 and not changed and not verified
    if bad:
        verdict, reason = "warn", "宣稱的檔案查無實證(找不到/空檔)"
    elif bluff:
        verdict, reason = "warn", f"嘴上 {actions} 個動作,磁碟 0 檔變動(疑似裝忙)"
    elif actions or claims or changed:
        verdict, reason = "ok", f"{len(changed)} 檔實際變動 · {len(verified)} 個宣稱有實證"
    else:
        verdict, reason = "none", ""
    return {"ts": int(time.time()), "engine": engine, "claims": claims,
            "actions": actions, "changed": len(changed),
            "bad": len(bad), "verdict": verdict, "reason": reason}


def record_behavior(engine, text, cwd, changed, write):
    rec = check_honesty(engine, text, cwd, changed, write)
    # 兜圈子:連續「宣稱動作但 0 變動」累加;真的動到檔或這輪沒宣稱就歸零
    if write:
        if rec["actions"] >= 1 and rec["changed"] == 0:
            STREAK[engine] = STREAK.get(engine, 0) + 1
        else:
            STREAK[engine] = 0
    rec["streak"] = STREAK.get(engine, 0)
    rec["circling"] = rec["streak"] >= 3
    if rec["circling"]:
        rec["verdict"] = "warn"
        rec["reason"] = f"連續 {rec['streak']} 輪宣稱進度但磁碟 0 變動(鬼打牆)"
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
                "claude_cwd": STATE["claude"]["cwd"], "codex_cwd": STATE["codex"]["cwd"]}))
        elif u.path == "/api/agent-config":
            self._send(200, json.dumps(AGENT_CFG))
        elif u.path == "/api/tokens":
            now = time.time()
            if _TOK_CACHE["data"] and now - _TOK_CACHE["ts"] < 10:
                self._send(200, json.dumps(_TOK_CACHE["data"]))
            else:
                d = token_stats()
                _TOK_CACHE.update(ts=now, data=d)
                self._send(200, json.dumps(d))
        elif u.path == "/api/behavior":
            self._send(200, json.dumps({"records": BEHAVIOR, "streak": STREAK}))
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
        elif self.path == "/api/project":
            path = (req.get("path") or "").strip()
            if path and not os.path.isdir(os.path.expanduser(path)):
                self._send(400, json.dumps({"error": f"目錄不存在: {path}"})); return
            path = os.path.expanduser(path) if path else None
            with LOCK:
                changed = path is not None and path != SETTINGS["project"]
                SETTINGS["project"] = path
                if changed:  # 只有「換專案」才讓兩個 agent 在新目錄重新開始
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
                    if k in req:
                        AGENT_CFG[eng][k] = req[k] or ("default" if k == "mode" and eng == "claude" else ("read-only" if k == "mode" else ""))
            self._send(200, json.dumps({"ok": True, "cfg": AGENT_CFG[eng]}))
        elif self.path == "/api/clear-context":
            # 清空 cache=重置該 agent 的 session,下一則用新 session 開始,
            # 不再背著累積的上下文重複 cache(等同對 Claude Code 送 /clear)
            eng = req.get("engine")
            engines = ["claude", "codex"] if eng in (None, "both") else [eng]
            with LOCK:
                for e in engines:
                    if e in STATE:
                        STATE[e]["id"] = None
                        STREAK[e] = 0
            self._send(200, json.dumps({"ok": True, "cleared": engines}))
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
        f"Code Duo 跑起來了  ->  http://localhost:{port}",
        f"  平台: {SYS}",
        f"  Claude CLI : {CLAUDE_BIN or '✗ 找不到(設 DUO_CLAUDE_BIN 或裝 Claude Code)'}",
        f"  Claude 標題: {'桌面 App 索引' if CLAUDE_SESS else 'CLI projects jsonl(aiTitle)'}",
        f"  Codex CLI  : {CODEX_BIN or '✗ 找不到(設 DUO_CODEX_BIN 或裝 Codex)'}",
        f"  Codex home : {CFG['codex_home']}",
    ]
    print("\n".join(banner), flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
