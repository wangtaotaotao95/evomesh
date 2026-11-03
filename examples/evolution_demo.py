"""No model/API key needed: two instances propose, validate and integrate a change."""

import sys
import tempfile
from pathlib import Path

from evomesh.evolution import Integrator, git, make_change, revision, submit
from evomesh.store import Store

with tempfile.TemporaryDirectory(prefix="evomesh-demo-") as directory:
    root = Path(directory).resolve()
    repo = root / "agent"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "SOUL.md").write_text("Be helpful.\n")
    (repo / "tool.py").write_text("def normalize(text):\n    return text\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Initial agent")
    store = Store(f"sqlite:///{root / 'ledger.db'}")
    store.initialize()
    base = revision(repo)
    for instance, target, path, content in [
        ("laptop", "soul", "SOUL.md", "Be helpful. Prefer concise answers.\n"),
        ("server", "tool", "tool.py", "def normalize(text):\n    return text.strip()\n"),
    ]:
        identifier = submit(
            store,
            repo,
            project="demo",
            instance=instance,
            target=target,
            rationale="Improve repeated task friction",
            changes=[make_change(repo, base, path, content)],
        )
        print(f"{instance} submitted {identifier} ({target})")
    worker = Integrator(
        store,
        repo,
        "demo",
        [
            [
                sys.executable,
                "-c",
                "from pathlib import Path; compile(Path('tool.py').read_text(), "
                "'tool.py', 'exec'); assert 'helpful' in Path('SOUL.md').read_text()",
            ]
        ],
    )
    for _ in range(2):
        result = worker.once()
        assert result["status"] == "integrated", result
        print("Integrated:", result["commit"])
    assert revision(repo) == base
    git(repo, "merge", "--ff-only", "evolution/main")
    assert "concise" in (repo / "SOUL.md").read_text()
    assert "strip()" in (repo / "tool.py").read_text()
    print("Upgrade complete: both instances' improvements are now in the agent.")
    store.close()
