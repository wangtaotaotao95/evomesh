"""Disposable offline app factory for `evomesh evaluate`. No production credentials."""

from tempfile import TemporaryDirectory

from evomesh import AgentApp, Config


class EvaluationApp(AgentApp):
    def __init__(self):
        from pathlib import Path

        self.directory = TemporaryDirectory()
        root = Path(self.directory.name)
        super().__init__(
            Config(
                database_url=f"sqlite:///{root / 'ledger.db'}",
                repo=root,
                workspace=root / "workspace",
                demo=True,
            )
        )
        (root / "workspace" / "notes.md").write_text("EvoMesh evolves its whole stack.\n")

    def close(self):
        super().close()
        self.directory.cleanup()


def create_app():
    return EvaluationApp()
