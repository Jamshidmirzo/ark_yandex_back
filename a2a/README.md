# A2A — ark_yandex backend ↔ ark_flutter_3 client

Lets the **Flutter developer's Claude Code** (on another machine, the
`ark_flutter_3` repo) ask questions, report bugs and request features from
**your backend Claude Code** (this machine, `ark_yandex`) over the
[A2A protocol](https://github.com/google/A2A).

```
Flutter dev's machine                     YOUR machine (this one)
─────────────────────                     ───────────────────────
Flutter Claude Code                       ark_yandex + your Claude Code
      │                                         ▲   │
      │  QUESTION / BUG / FEATURE / LIST        │   │ claude --print
      ▼                                         │   │ (answers / edits)
 a2a-bridge MCP ───── HTTP ─────► a2a/server.py :9999
                                          │
                                  a2a/tasks.json
```

- **`QUESTION:`** — server runs `claude --print <q>` in `ark_yandex/` and
  returns a grounded answer **synchronously**.
- **`BUG:` / `FEATURE:`** — appended to `a2a/tasks.json`, replies immediately.
  With `--autorun` it also spawns a headless `claude` to start editing the repo;
  by default it just queues the task for you to pick up in your own session.
- **`LIST`** — returns all tasks with `status != done`.

---

## Setup — YOUR side (this machine)

You need `claude` on `PATH` (`claude --version` works) and Python 3.11+.

```bash
cd /Users/user/Desktop/intersoft/ark_yandex

# 1. Separate venv for the A2A tool (keep it off the Django venv)
python3.13 -m venv a2a/.venv
source a2a/.venv/bin/activate
pip install -r a2a/requirements.txt

# 2. Find your LAN IP (same Wi-Fi as the Flutter dev)
ipconfig getifaddr en0        # e.g. 192.168.0.106

# 3. Run, advertising that IP in the agent card
python a2a/server.py --port 9999 --public-url http://192.168.0.106:9999
```

`--project` defaults to this repo, so you don't need to pass it. Add
`--autorun` only if you want BUG/FEATURE to immediately drive headless,
repo-editing `claude` runs (see caveats).

## Network — getting the Flutter dev's machine to reach you

- **Same Wi-Fi / LAN:** use your IP directly — `http://192.168.0.106:9999`.
- **Different networks:** open a tunnel and share the public URL instead:
  ```bash
  ngrok http 9999                       # → https://abc.ngrok-free.app
  # or:  cloudflared tunnel --url http://localhost:9999
  ```
  Then start the server with `--public-url https://abc.ngrok-free.app`.

## Smoke test (run on YOUR machine first)

```bash
# Agent card — the "url" must be your public URL, NOT http://0.0.0.0:9999
curl http://192.168.0.106:9999/.well-known/agent-card.json

# A question end-to-end
curl -X POST http://192.168.0.106:9999 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"role":"user","messageId":"t1","parts":[{"type":"text","text":"QUESTION: What API endpoints exist under /api/v1/car-orders/?"}]}}}'
```

## Hand the Flutter dev `CLAUDE_FRONTEND.md`

Send them [CLAUDE_FRONTEND.md](CLAUDE_FRONTEND.md) — it has the one-line MCP
registration (`claude mcp add a2a-bridge …`) plus the rules to drop into their
Flutter repo as `CLAUDE.md`. Give them your `--public-url` to paste in.

---

## Caveats (read this)

1. **No auth.** The server binds `0.0.0.0` and accepts any caller — anyone who
   can reach port `9999` can trigger `claude` runs in `ark_yandex`. Only run it
   on a trusted LAN, or behind an ngrok URL that's hard to guess, and shut it
   down when you're done.
2. **`--autorun` is powerful and flaky.** It spawns
   `claude --print --dangerously-skip-permissions` as a child of this process —
   it edits the repo unattended, doesn't show in a terminal, and may stall.
   Default is OFF: treat `tasks.json` as the real handoff and pick tasks up in
   your own Claude Code session. Turn `--autorun` on only when you trust it.
3. **No file locking on `tasks.json`.** Fine for low volume; don't hammer it.
4. **`QUESTION` needs read access.** `claude --print` answers using the
   default-allowed read tools. If answers come back empty because reads were
   blocked, add `--dangerously-skip-permissions` to the QUESTION call in
   `ask_claude_about_codebase` (it's read-only Q&A).
