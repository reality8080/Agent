# Agent System v0.5 — LangGraph Integration

Bản tích hợp Lean theo `Agent_System_Spec_v0.5.html`. LangGraph là workflow controller duy nhất; các adapter Laya, LLM, Tavily và MemoryLake hiện có được tái sử dụng, không viết lại.

## Workflow

```text
Receive → Laya Shadow → Search + Workspace Retrieval → MemoryLake Retrieval
        → Planner (read-only) → Gate 1 interrupt/resume
        → Coder → Test Runner → Reviewer (read-only)
        → Gate 2 only when privileged actions exist → Deliver → Memory Proposal
```

- **Laya** trả typed shadow decision (task type, risk, priority, route suggestion, confidence, abstain); không routing hoặc cấp quyền.
- **Search** tái sử dụng `TavilySearchAdapter`, giữ `title`, `url`, `content`, score và mock/live flag. Mock search không được coi là external evidence. Search node đồng thời đọc repository trong `WORKSPACE_ROOT` bằng standard library, giới hạn file/suffix/kích thước và không đi ra ngoài workspace.
- **MemoryLake** tái sử dụng `MemoryLakeAdapter`; truy xuất theo `MEMORYLAKE_PROJECT_ID`, giữ provenance/status/conflicts; proposal được tạo sau Deliver ở trạng thái `proposed` hoặc `disputed`, không tự promote.
- **Main LLM**: một model ID (`LLM_MODEL`) được pin cho Plan, Coding và read-only Reviewer. Permission mode khác nhau; Planner không có write capability, Coder chỉ chạy sau Gate 1.
- **Gate 1**: `langgraph.types.interrupt` + `Command(resume=...)`, SQLite checkpointer, ràng buộc `plan_version` + `scope_hash` + `environment_id`, HMAC token. Luôn yêu cầu human approval ở mọi environment; không có đường auto-approve.
- **Gate 2**: route chỉ vào gate khi Planner phát hiện privileged actions (destructive, system install, push/merge, SSH/deploy/production). Không hỏi lại cho task coding thông thường. Trong phạm vi MVP này Gate 2 chỉ ghi nhận approval; privileged-action executor không được triển khai và deliverable luôn báo `approved_not_executed`.
- **Tool Executor**: tính lại scope hash cùng canonical workspace trước mỗi write; chặn absolute/traversal/symlink escape; từ chối overwrite dirty/untracked/non-Git file; yêu cầu Gate 1 hợp lệ trước từng write. MOCK/dry-run không ghi file và không thực thi tool calls.
- **Git**: chỉ đọc `status`, tracked files và diff; không tự commit/push/reset/checkout. Workspace paths được chuẩn hóa tương đối và loại prefix `workspace/` thừa.
- **Docker Test Runner**: mặc định chỉ chạy test trong workspace qua container non-root, read-only workspace mount, network disabled, CPU/RAM/PID/time limits. Không có test hoặc không có Docker thì `not_run`, không phải PASS.
- **Reviewer**: read-only; đối chiếu plan, diff, changed paths, tests, scope binding và Ponytail instructions. Model score chỉ là thông tin, không thể cấp PASS khi deterministic evidence chưa đạt.
- **SQLite Event Ledger**: DB riêng, append-only trigger + SHA-256 hash-chain; action state dùng stable `action_id` và started/completed/failed. Không ghi credential/token.

## Windows PowerShell — cài đặt

Dùng Python 3.11+ (khuyến nghị 3.11/3.12 cho ecosystem dependencies) và Docker Desktop nếu muốn chạy test isolation.

```powershell
cd .\Agent-main
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\src\adapters\requirements-adapters.txt
Copy-Item .env.example .env
```

Sinh HMAC secret cho Gate 1 rồi ghi vào `.env`:

```powershell
$bytes = New-Object byte[] 48
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$secret = [Convert]::ToBase64String($bytes)
(Get-Content .env) -replace '^APPROVAL_HMAC_SECRET=.*', "APPROVAL_HMAC_SECRET=$secret" | Set-Content .env
```

Gate 1 luôn cần người dùng resume/approve; `GATE1_AUTO_APPROVE` không được sử dụng. Mặc định `WORKSPACE_ROOT=workspace` là sandbox riêng. Khi thao tác trên repository có sẵn, chỉ rõ đường dẫn repository bằng `--workspace` hoặc `WORKSPACE_ROOT`; Git status sẽ được chụp trước khi Coding. Không đặt API keys trong source hoặc Git.

## Windows PowerShell — chạy mock an toàn và Gate 1 pause/resume

Đặt trong `.env` hoặc session PowerShell:

```powershell
$env:LLM_MOCK = "1"
$env:LAYA_MOCK = "1"
$env:SEARCH_MOCK = "1"
$env:MEMORYLAKE_MOCK = "1"
$env:AGENT_TEST_DOCKER = "1"
```

Build test image (một lần; Docker cần tải các package ở bước build):

```powershell
docker build -t agent-system-test:local .
```

Bắt đầu task. Ví dụ dùng workspace sandbox mặc định (để chạy trên repository có sẵn, truyền `--workspace "D:\Projects\TargetRepo"`). Lệnh trả về `waiting_approval` cùng `thread_id`, `plan_version`, `scope_hash`, `environment_id`:

```powershell
$result = python .\run_agent.py "Create a minimal hello.py module and tests inside the workspace" | ConvertFrom-Json
$result | Format-List
$thread = $result.thread_id
```

Duyệt đúng plan hiện tại và resume cùng thread:

```powershell
python .\run_agent.py --resume --thread-id $thread --approve --approved-by "project-owner"
```

Từ chối thay vì duyệt:

```powershell
python .\run_agent.py --resume --thread-id $thread --reject --reason "Plan scope needs revision"
```

Muốn xem state và plan đã checkpoint, gọi:

```powershell
python -c "import sys,json; sys.path.insert(0,'src'); from agent_system.graph import get_workflow_state; print(json.dumps(get_workflow_state('$thread'),indent=2,ensure_ascii=False,default=str))"
```

Khi Gate 2 có privileged action, lần resume bổ sung sẽ xuất hiện sau Review; cùng cú pháp `--approve`/`--reject` được dùng và quyết định được bind với action list.

## Live configuration

Sau khi mock tests đạt, đặt `LLM_MOCK=0`, `SEARCH_MOCK=0`, `MEMORYLAKE_MOCK=0`. Cấu hình đúng một model ID qua `LLM_MODEL`; không dùng `LLM_MODEL_PLAN`/`LLM_MODEL_CODING` để tách model. `TAVILY_API_KEY` và `MEMORYLAKE_PROJECT_ID` là tùy chọn theo nhu cầu. MemoryLake MCP dùng OAuth 2.1 qua browser; không dùng Personal REST API Key làm MCP Bearer token. Xem `docs/MEMORYLAKE_MCP_SETUP.md`. `LAYA_MOCK=0` chỉ hoạt động khi cài được Laya CPU dependency tương thích.

```dotenv
LLM_PROVIDER=gemini
LLM_MODEL=gemini/gemini-3.5-flash-lite
LLM_MOCK=0
TAVILY_API_KEY=...
SEARCH_MOCK=0
MEMORYLAKE_MCP_URL=https://app.memorylake.ai/memorylake/mcp/v2
MEMORYLAKE_PROJECT_ID=...
MEMORYLAKE_MOCK=0
MEMORYLAKE_OAUTH_CALLBACK_PORT=8765
LAYA_MOCK=0
```

## Tests và kiểm tra evidence

Kiểm tra nhanh source + unit tests (chạy trong môi trường phát triển, không phải Test Runner sandbox):

```powershell
python -m compileall -q .\src .\run_agent.py
python -m pytest .\src\tests -q
```

Test Runner của Agent chỉ tìm `tests/test_*.py` và `src/tests/test_*.py` bên trong workspace đã chọn; không fallback ra thư mục ngoài workspace. Container mặc định mount workspace read-only và không có network:

```powershell
$env:AGENT_TEST_DOCKER = "1"
$env:AGENT_DOCKER_IMAGE = "agent-system-test:local"
```

Dùng `AGENT_TEST_DOCKER=0` chỉ cho trusted local workspace; local subprocess không có mức cô lập tương đương Docker.

Event Ledger integrity có thể kiểm tra bằng Python:

```powershell
python -c "import sys; sys.path.insert(0,'src'); from agent_system.storage.event_ledger import EventLedger; print(EventLedger().verify_integrity())"
```

## Package layout

```text
AGENTS.md
.env.example
Dockerfile
.devcontainer/devcontainer.json
run_agent.py
src/
  adapters/               # existing LLM / Tavily / MemoryLake adapters
  Laya/                   # existing Laya adapter and shadow integration
  agent_system/
    graph.py              # sole LangGraph controller + resume API
    runtime.py            # shared one-model / adapter factories
    workspace.py          # canonical root, path and Git helpers
    nodes/                # workflow nodes (no extra agents)
    security/             # HMAC approval + scoped tool executor
    storage/              # SQLite checkpointer + independent event ledger
  tests/
```

## Scope exclusions and known limits

- No NCM, subagents, Remote SSH, production deployment, commit, push, reset or automatic deploy.
- The coding interface accepts bounded `FILE:` full-file patches; LLM tool-call payloads are never executed. This is deliberate until a separate allowlisted tool protocol is approved.
- Existing file overwrite requires a clean tracked Git file. If workspace is not a Git repository, Coder may create new files but will not overwrite existing files.
- SQLite checkpointer is intended for local/small workflows, not concurrent production scale.
- Live provider/MemoryLake credentials, live tool schemas, Laya weights and Docker runtime must be verified on the target Windows host.
