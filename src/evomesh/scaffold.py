"""Generate independent, runnable assistant and framework projects."""

from pathlib import Path

CONFIG = """[evomesh]
project = "my-agent"
owner = "personal"
database_url = "sqlite:///.evomesh/ledger.db"
workspace = "workspace"
repo = "."
demo = true
max_steps = 12
history_turns = 20
terminal = "disabled"
allowed_hosts = []
# For a real model: demo = false, then set model and base_url here or via environment.
# Keep EVOMESH_API_KEY in your environment, never in source control.
# model = "your-tool-capable-model"
# base_url = "https://your-provider.example/v1"
"""


def scaffold(destination: Path, template: str = "assistant") -> None:
    if template not in {"assistant", "framework"}:
        raise ValueError("Unknown template")
    if destination.exists():
        raise ValueError("Destination already exists; choose a new directory")
    destination.mkdir(parents=True)
    files = {
        "evomesh.toml": CONFIG,
        ".gitignore": ".evomesh/\nworkspace/\n.venv/\n__pycache__/\n.env\n",
        "SOUL.md": "# My agent\n\nBe helpful, concise, and honest. Learn from observed friction.\n",
        "README.md": "# My EvoMesh agent\n\nRequires the installed EvoMesh package.\n\n"
        "```bash\nevomesh console\nevomesh chat --session main\n"
        "evomesh run --session main 'List files'\n"
        "evomesh task submit 'List files'\nevomesh task-worker --once\n"
        "evomesh task list\n```\n\nThe generated configuration uses an offline demo model. "
        "For real inference, set demo=false and configure your model endpoint. "
        "Run `evomesh console` for the web UI and embedded worker.\n"
        "Use `evomesh serve` with EVOMESH_SERVER_TOKEN set for a separate web/API replica.\n",
    }
    if template == "framework":
        files["app.py"] = '''from evomesh import AgentApp, tool

@tool
def word_count(text: str) -> int:
    """Count words in text."""
    return len(text.split())

with AgentApp.from_config("evomesh.toml", tools=[word_count]) as app:
    print(app.run("List my workspace", session="main"))
    task_id = app.submit("List my workspace", key="example-list")
    app.work_once()
    print(app.state.task(task_id))
'''
    for name, content in files.items():
        (destination / name).write_text(content, encoding="utf-8")
