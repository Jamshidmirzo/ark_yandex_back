# CLAUDE.md — ark_flutter_3 client (A2A frontend side)

Give this file to the **Flutter developer**. They:

1. Register the A2A bridge MCP (one command, needs Node + `npx`):

   ```bash
   claude mcp add a2a-bridge \
     -e A2A_AGENT_URLS=<BACKEND_URL> \
     -- npx -y a2a-mcp-bridge
   ```

   `<BACKEND_URL>` is the `--public-url` you started the server with, e.g.
   `http://192.168.0.106:9999` (same LAN) or `https://abc.ngrok-free.app`
   (ngrok). To change it later: `claude mcp remove a2a-bridge`, then re-add.

2. Save the section below as `CLAUDE.md` at the **root of the `ark_flutter_3`
   repo**.

3. Verify, inside Claude Code in the Flutter repo:
   - `Use a2a-bridge: call list_agents` → should show "ARK Yandex Backend
     Agent" with a slug (e.g. `ark-yandex-backend-agent`). Cache that slug.
   - `Use a2a-bridge send_message — agent: <slug>, message: "LIST"` → `No open tasks.`
   - `Use a2a-bridge send_message — agent: <slug>, message: "QUESTION: What endpoints exist under /api/v1/car-orders/?"`
     → a grounded list. If yes, it works.

---

## (copy from here down into ark_flutter_3/CLAUDE.md)

You are the **frontend** Claude for `ark_flutter_3`. The Django backend
(`ark_yandex`) lives on another machine and has its own Claude Code. You reach
it through the `a2a-bridge` MCP, pre-configured via `A2A_AGENT_URLS`.

### MCP tools

- `list_agents` — run once at session start to discover the agent slug; cache it.
- `send_message` — main tool. Args: `agent` (the slug), `message` (your text).
  Optional `context_id` to continue a thread.
- `get_agent_card`, `get_task`, `refresh_agents` — rarely needed.

Call `send_message` whenever you need something the backend owns. `message`
must follow one of these formats:

### 1. You need to know how the backend works
```
QUESTION: <specific question>
```
Examples:
- `QUESTION: What fields does GET /api/v1/auth/me/ return?`
- `QUESTION: Is /api/v1/car-orders paginated, and what query params filter it?`
- `QUESTION: Which WS topics push live driver location and trip_state?`

The backend Claude runs `claude --print` against the real `ark_yandex` code and
returns a grounded answer. Use it before writing Flutter code that assumes a shape.

### 2. You found a backend bug
```
BUG: <title> | <what happened> | <endpoint or file>
```
Example:
`BUG: driver_location null mid-trip | heartbeat not reflected on detail | GET /api/v1/car-orders/{id}/`

### 3. You need an endpoint that doesn't exist yet
```
FEATURE: <title> | <what is needed and why> | <expected response/behavior>
```
Example:
`FEATURE: ETA on order detail | tracking screen needs arrival estimate | GET /api/v1/car-orders/{id}/ → add eta_minutes`

### 4. Check what's queued
```
LIST
```

### Rules

- Always ask the backend agent **before** inventing an API shape or telling the
  developer "the backend is missing X" — it may exist under another name.
- Don't block Flutter progress on a fix. Mock the data and leave a
  `// TODO(a2a #<task-id>)` referencing the task id the backend replies with.
- One issue per message. Be concrete: include method, path, and the exact
  shape you expect.
- `QUESTION` is synchronous (you get an answer). `BUG`/`FEATURE` are async — you
  get a task id back; the work happens on the backend side.
