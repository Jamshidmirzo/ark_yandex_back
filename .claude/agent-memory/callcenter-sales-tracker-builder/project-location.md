---
name: naffAI project location
description: Where the naffAI sales tracker code lives and why it is separate from ark_yandex
type: project
---

naffAI is the standalone callcenter sales tracker we built turnkey. The codebase lives at
`/Users/user/Desktop/mp/naffAI` (its own git repo, branch `main`), not inside the
`intersoft/ark_yandex` working tree.

Why: the user explicitly asked us to place the project there as a turnkey deliverable; it
shares nothing with the ark gateway/standalone wiring, so co-locating would invite confusion
with the intersoft multi-repo layout already documented in user memory.

How to apply: when continuing work on this project, `cd /Users/user/Desktop/mp/naffAI`. The
`backend/` Python venv is at `backend/.venv` (Python 3.12 via uv). Frontend is `frontend/`
(Vite + React). Docker Compose lives at the repo root.
