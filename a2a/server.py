"""
A2A Backend Agent Server — ark_yandex
-------------------------------------
Lets the Flutter developer's Claude Code (on ANOTHER machine) talk to THIS
backend's Claude Code over the Google A2A protocol. Handles four message types:

  QUESTION: <question>
      → Runs `claude --print` against the ark_yandex codebase and returns a
        grounded answer (synchronous). Use this so the Flutter side stops
        guessing API shapes.

  BUG: <title> | <description> | <endpoint or file>
      → Saves to tasks.json. With --autorun, also spawns headless Claude Code
        to start fixing it. Without --autorun (default), you pick it up yourself
        in your own Claude Code session.

  FEATURE: <title> | <description> | <expected behavior>
      → Same as BUG, for a missing endpoint/feature.

  LIST
      → Returns all tasks with status != done.

Run (from this repo root, in the a2a venv — see a2a/README.md):
    python a2a/server.py --port 9999 --public-url http://<YOUR_LAN_IP>:9999
"""

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)
from a2a.utils import new_agent_text_message

# ── Config ────────────────────────────────────────────────────────────────────
# a2a/ lives inside the repo, so the project root is one level up.
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent
TASKS_FILE = Path(__file__).parent / "tasks.json"
LOGS_DIR = Path(__file__).parent / "task-logs"
LOGS_DIR.mkdir(exist_ok=True)

# Set by main() from the --autorun flag.
AUTORUN = False

# ── Task storage ──────────────────────────────────────────────────────────────

def load_tasks() -> list:
    if TASKS_FILE.exists():
        return json.loads(TASKS_FILE.read_text())
    return []

def save_task(task: dict):
    tasks = load_tasks()
    tasks.append(task)
    TASKS_FILE.write_text(json.dumps(tasks, indent=2))
    print(f"\n📥  [{task['type'].upper()}] #{task['id']} — {task['title']}")

def update_task_status(task_id: str, status: str, **extra):
    tasks = load_tasks()
    now = datetime.now(timezone.utc).isoformat()
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = status
            if status == "in_progress" and "started_at" not in t:
                t["started_at"] = now
            if status in ("done", "failed"):
                t["resolved_at"] = now
            t.update(extra)
    TASKS_FILE.write_text(json.dumps(tasks, indent=2))

# ── Claude Code integration ───────────────────────────────────────────────────

async def ask_claude_about_codebase(question: str, project_root: Path) -> str:
    """Runs `claude --print "<question>"` in ark_yandex and returns the answer."""
    print(f"\n🤖  Flutter Claude asks: {question}")
    print(f"    Running Claude Code in: {project_root}")

    try:
        process = await asyncio.create_subprocess_exec(
            "claude", "--print", question,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_root),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)

        if process.returncode == 0 and stdout:
            answer = stdout.decode().strip()
            print(f"    ✅ Got answer ({len(answer)} chars)")
            return answer
        else:
            err = stderr.decode().strip() if stderr else "no error output"
            print(f"    ⚠️  Claude Code error: {err}")
            return f"Claude Code could not answer (exit {process.returncode}): {err}"

    except asyncio.TimeoutError:
        return "Claude Code timed out after 120 seconds. The question may be too complex."
    except FileNotFoundError:
        return (
            "Claude Code CLI not found. Make sure `claude` is installed and in PATH.\n"
            "Install: https://docs.anthropic.com/en/docs/claude-code"
        )

async def trigger_claude_for_task(task: dict, project_root: Path):
    """
    Launches Claude Code headlessly to work on a bug/feature task.
    Uses --print --dangerously-skip-permissions so it runs unattended.
    Output is streamed to task-logs/<id>.log for live tailing.
    Only called when the server is started with --autorun.
    """
    prompt = _build_task_prompt(task)
    log_path = LOGS_DIR / f"{task['id']}.log"
    print(f"\n🚀  Triggering Claude Code for task #{task['id']} → {log_path}")

    update_task_status(task["id"], "in_progress", log=str(log_path))

    async def run():
        try:
            with log_path.open("wb") as logf:
                logf.write(f"# task {task['id']} — {task['title']}\n".encode())
                logf.write(f"# prompt:\n{prompt}\n\n# --- claude output ---\n".encode())
                logf.flush()
                process = await asyncio.create_subprocess_exec(
                    "claude",
                    "--print",
                    "--dangerously-skip-permissions",
                    prompt,
                    cwd=str(project_root),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=logf,
                    stderr=asyncio.subprocess.STDOUT,
                )
                rc = await process.wait()
            if rc == 0:
                update_task_status(task["id"], "done")
                print(f"    ✅ task #{task['id']} finished (exit 0)")
            else:
                update_task_status(task["id"], "failed", exit_code=rc)
                print(f"    ❌ task #{task['id']} failed (exit {rc}) — see {log_path}")
        except FileNotFoundError:
            update_task_status(task["id"], "failed", error="claude CLI not found")
            print("    ⚠️  Claude Code CLI not found — install it on PATH.")
        except Exception as e:
            update_task_status(task["id"], "failed", error=str(e))
            print(f"    ⚠️  task #{task['id']} crashed: {e}")

    asyncio.create_task(run())

def _build_task_prompt(task: dict) -> str:
    if task["type"] == "bug":
        return (
            f"The Flutter frontend team (ark_flutter_3) found a bug in the "
            f"ark_yandex backend. Please investigate and fix it.\n\n"
            f"Title: {task['title']}\n"
            f"Description: {task['description']}\n"
            f"Location (endpoint/file): {task.get('location', 'unknown')}\n\n"
            f"Task ID: {task['id']} — update a2a/tasks.json status to 'done' when resolved."
        )
    else:
        return (
            f"The Flutter frontend team (ark_flutter_3) needs a new ark_yandex "
            f"backend feature. Please implement it.\n\n"
            f"Title: {task['title']}\n"
            f"Description: {task['description']}\n"
            f"Expected behavior: {task.get('expected_behavior', '')}\n\n"
            f"Task ID: {task['id']} — update a2a/tasks.json status to 'done' when resolved."
        )

# ── Agent Executor ────────────────────────────────────────────────────────────

class BackendAgentExecutor(AgentExecutor):

    def __init__(self, project_root: Path):
        self.project_root = project_root

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        text = ""
        for part in context.message.parts:
            if hasattr(part, "root") and hasattr(part.root, "text"):
                text = part.root.text.strip()
                break
            elif hasattr(part, "text"):
                text = part.text.strip()
                break

        reply = await self._handle(text)
        await event_queue.enqueue_event(new_agent_text_message(reply))

    async def _handle(self, text: str) -> str:
        upper = text.upper()

        # ── QUESTION ──────────────────────────────────────────────────────────
        if upper.startswith("QUESTION:"):
            question = text[9:].strip()
            if not question:
                return "Please provide a question after 'QUESTION:'"
            answer = await ask_claude_about_codebase(question, self.project_root)
            return f"[ark_yandex backend Claude answers]\n\n{answer}"

        # ── BUG ───────────────────────────────────────────────────────────────
        elif upper.startswith("BUG:"):
            parts = [p.strip() for p in text[4:].split("|")]
            task = {
                "id": str(uuid.uuid4())[:8],
                "type": "bug",
                "status": "pending",
                "title": parts[0] if parts else "Untitled",
                "description": parts[1] if len(parts) > 1 else "",
                "location": parts[2] if len(parts) > 2 else "",
                "reported_at": datetime.now(timezone.utc).isoformat(),
            }
            save_task(task)
            if AUTORUN:
                await trigger_claude_for_task(task, self.project_root)
                tail = "Backend Claude Code is starting to investigate."
            else:
                tail = "Queued in a2a/tasks.json — the backend dev will pick it up."
            return f"✅ Bug #{task['id']} logged: '{task['title']}'\n{tail}"

        # ── FEATURE ───────────────────────────────────────────────────────────
        elif upper.startswith("FEATURE:"):
            parts = [p.strip() for p in text[8:].split("|")]
            task = {
                "id": str(uuid.uuid4())[:8],
                "type": "feature",
                "status": "pending",
                "title": parts[0] if parts else "Untitled",
                "description": parts[1] if len(parts) > 1 else "",
                "expected_behavior": parts[2] if len(parts) > 2 else "",
                "reported_at": datetime.now(timezone.utc).isoformat(),
            }
            save_task(task)
            if AUTORUN:
                await trigger_claude_for_task(task, self.project_root)
                tail = "Backend Claude Code is starting to implement this."
            else:
                tail = "Queued in a2a/tasks.json — the backend dev will pick it up."
            return f"✅ Feature #{task['id']} logged: '{task['title']}'\n{tail}"

        # ── LIST ──────────────────────────────────────────────────────────────
        elif upper.startswith("LIST"):
            tasks = load_tasks()
            pending = [t for t in tasks if t["status"] != "done"]
            if not pending:
                return "No open tasks."
            lines = [
                f"[{t['status'].upper()}] #{t['id']} {t['type'].upper()}: {t['title']}"
                for t in pending
            ]
            return "Open tasks:\n" + "\n".join(lines)

        # ── UNKNOWN ───────────────────────────────────────────────────────────
        else:
            return (
                "Unknown format. Use one of:\n\n"
                "  QUESTION: <anything about the ark_yandex backend>\n"
                "  BUG: <title> | <description> | <endpoint or file>\n"
                "  FEATURE: <title> | <description> | <expected behavior>\n"
                "  LIST"
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        await event_queue.enqueue_event(new_agent_text_message("Cancelled."))

# ── Agent Card & Server ───────────────────────────────────────────────────────

def build_agent_card(public_url: str) -> AgentCard:
    return AgentCard(
        name="ARK Yandex Backend Agent",
        description=(
            "Backend dev agent for the ark_yandex car-orders gateway. Answers "
            "questions about the codebase (auth, car-orders, drivers, dispatch, "
            "the OrderMeta overlay, live tracking/WS), receives bug reports and "
            "implements missing features — all over A2A."
        ),
        url=public_url,
        version="1.0.0",
        skills=[
            AgentSkill(
                id="ask_about_backend",
                name="Ask About Backend",
                description="Ask anything about how the ark_yandex backend is implemented.",
                tags=["question", "backend", "codebase"],
                examples=[
                    "QUESTION: What does GET /api/v1/car-orders/{id}/ return while in_progress?",
                    "QUESTION: Which WS topics exist for live tracking?",
                    "QUESTION: How does overlay-claim differ from the upstream claim?",
                ],
            ),
            AgentSkill(
                id="report_bug",
                name="Report Bug",
                description="Report a backend bug for the ark_yandex team.",
                tags=["bug", "backend"],
                examples=[
                    "BUG: driver_location null mid-trip | heartbeat not persisted | POST /api/v1/car-orders/drivers/me/location/",
                ],
            ),
            AgentSkill(
                id="request_feature",
                name="Request Feature",
                description="Request a missing ark_yandex backend feature/endpoint.",
                tags=["feature", "backend"],
                examples=[
                    "FEATURE: ETA on order detail | profile screen needs arrival estimate | GET /api/v1/car-orders/{id}/ → add eta_minutes",
                ],
            ),
            AgentSkill(
                id="list_tasks",
                name="List Tasks",
                description="List all open A2A tasks.",
                tags=["list", "status"],
                examples=["LIST"],
            ),
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )

def main():
    global AUTORUN
    parser = argparse.ArgumentParser(description="A2A ark_yandex Backend Agent Server")
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT_ROOT,
                        help="Path to the backend project root (default: this repo)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument(
        "--public-url",
        help="Externally reachable URL advertised in the agent card "
             "(e.g. http://192.168.0.106:9999 or https://abc.ngrok-free.app). "
             "Required when --host is 0.0.0.0.",
    )
    parser.add_argument(
        "--autorun", action="store_true",
        help="On BUG/FEATURE, immediately spawn headless `claude "
             "--dangerously-skip-permissions` to edit the repo. Off by default "
             "(tasks are only queued in tasks.json for you to pick up).",
    )
    args = parser.parse_args()
    AUTORUN = args.autorun

    public_url = args.public_url
    if not public_url:
        if args.host in ("0.0.0.0", "::"):
            parser.error(
                "--public-url is required when binding to 0.0.0.0. "
                "Pass e.g. --public-url http://192.168.0.106:9999"
            )
        public_url = f"http://{args.host}:{args.port}"
    public_url = public_url.rstrip("/") + "/"

    project_root = args.project.resolve()
    print(f"🚀  ark_yandex A2A Agent binding on http://{args.host}:{args.port}")
    print(f"🌐  Public URL (advertised in agent card): {public_url}")
    print(f"📁  Backend project root: {project_root}")
    print(f"📋  Tasks file: {TASKS_FILE.resolve()}")
    print(f"🛠   Autorun (headless edits on BUG/FEATURE): {'ON' if AUTORUN else 'off'}")
    print(f"📡  Agent card: {public_url}.well-known/agent-card.json\n")

    agent_card = build_agent_card(public_url)
    request_handler = DefaultRequestHandler(
        agent_executor=BackendAgentExecutor(project_root=project_root),
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()

    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
