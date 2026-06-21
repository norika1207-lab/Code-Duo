# Code Duo

一個介面，同時跟 Claude 與 Codex 兩個 AI 工程師協作。`@claude` / `@codex` / `@both` 路由，兩邊並行回話、各自保留上下文，還能把過去的 session 接回來續聊。

重點：**不走 API，走訂閱。** 背後驅動的是你機器上已登入的 `claude` 與 `codex` CLI，認證沿用你的 Max / ChatGPT 訂閱，不需要也不會用到 API key。

## 功能

- 單一聊天介面，左 Claude、右 Codex，同時載入
- `@claude` / `@codex` / `@both` 指定對象，`@both` 兩顆並行
- 跨輪接續記憶（Claude `--resume`、Codex `exec resume`）
- 兩側歷史側欄：依專案分組、顯示官方對話標題、點一下即接續舊對話
- 每條對話的 ⋮ 選單：重新命名、置頂、封存、移除（duo 本地覆寫，不動官方 App 資料）

## 需求

- macOS
- Claude Code CLI（`claude`，已用訂閱登入）
- Codex（`/Applications/Codex.app`，已用 ChatGPT 訂閱登入）
- Python 3（標準庫即可，無第三方套件）

## 啟動

```bash
./start.sh
# 或
python3 app.py
```

開瀏覽器到 http://localhost:8765

## 架構

- `app.py` — 純標準庫 HTTP server，把兩個 CLI 當引擎以無頭模式驅動
- `index.html` — 前端介面（vanilla JS）
- `logo.svg` — 標誌

標題與專案分組讀自各 App 的官方索引：Claude 讀 `~/Library/Application Support/Claude/claude-code-sessions/`，Codex 讀 `~/.codex/session_index.jsonl` 與 `~/.codex/.codex-global-state.json`。
