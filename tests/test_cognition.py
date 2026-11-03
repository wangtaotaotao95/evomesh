import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from evomesh import Agent, AgentApp, Config
from evomesh.agent import RepeatedToolCall
from evomesh.cognition import CognitionStore, SkillLibrary
from evomesh.context import ContextBudgetExceeded, fit_context, request_size
from evomesh.runtime_store import RuntimeStore, SessionBusy, sessions
from evomesh.store import LeaseLost
from evomesh.tools import Tool, object_schema


def test_plans_persist_with_versions_and_owner_isolation(store, project, tmp_path):
    state = RuntimeStore(store, project)
    cognition = CognitionStore(state, tmp_path)
    result = cognition.save_plan(
        "main", "Prepare report", [{"title": "Read notes", "status": "todo"}]
    )
    assert result["version"] == 1
    other = CognitionStore(RuntimeStore(store, project), tmp_path)
    assert other.plan("main")["title"] == "Prepare report"
    with pytest.raises(ValueError, match="exists"):
        other.save_plan("main", "overwrite", [{"title": "bad", "status": "todo"}])
    assert (
        cognition.save_plan(
            "main", "Prepare report", [{"title": "Read notes", "status": "done"}], 1
        )["version"]
        == 2
    )
    with pytest.raises(ValueError, match="changed"):
        other.save_plan("main", "stale", [{"title": "bad", "status": "todo"}], 1)
    isolated = CognitionStore(RuntimeStore(store, project, "other"), tmp_path)
    assert isolated.list_plans() == [] and isolated.plan("main") is None


def test_plan_session_fencing_and_concurrent_edit(store, project, tmp_path):
    state = RuntimeStore(store, project)
    cognition = CognitionStore(state, tmp_path)
    token, _ = state.acquire_session("main")
    with pytest.raises(SessionBusy):
        cognition.save_plan("main", "Blocked", [{"title": "step", "status": "todo"}])
    cognition.save_plan("main", "Safe", [{"title": "step", "status": "doing"}], lease=token)
    with store.engine.begin() as conn:
        conn.execute(update(sessions).where(sessions.c.project == project).values(lease_until=0))
    with pytest.raises(LeaseLost):
        cognition.save_plan("main", "Stale", [{"title": "step", "status": "done"}], 1, lease=token)
    with pytest.raises(LeaseLost):
        state.release_session("main", token)

    def change(_):
        try:
            cognition.save_plan("main", "New", [{"title": "step", "status": "done"}], 1)
            return True
        except (SessionBusy, ValueError):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(change, range(2))) == 1


def test_cited_retrieval_chinese_and_invalidated_documents(store, project, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("# Project\nAgent 全域进化记录\nSQLite and MySQL storage\n")
    cognition = CognitionStore(RuntimeStore(store, project), root)
    indexed = cognition.index_document("notes.md")
    assert indexed["chunks"] == 1
    hits = cognition.search("全域进化")
    assert hits[0]["path"] == "notes.md" and hits[0]["start_line"] == 1
    assert hits[0]["end_line"] == 3 and "全域进化" in hits[0]["text"]
    assert cognition.search("MySQL")[0]["digest"] == indexed["digest"]
    other = CognitionStore(RuntimeStore(store, project, "other"), root)
    assert other.search("MySQL") == [] and other.list_documents() == []
    (root / "notes.md").write_text("New replacement")
    assert cognition.search("MySQL") == []
    cognition.index_document("notes.md")
    assert cognition.search("replacement")
    (root / "notes.md").unlink()
    assert cognition.search("replacement") == []
    document = cognition.list_documents()[0]
    assert not other.remove_document(document["id"])
    assert cognition.remove_document(document["id"])


def test_retrieval_boundaries_and_symlink_retarget(store, project, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("private")
    cognition = CognitionStore(RuntimeStore(store, project), root)
    (root / ".env").write_text("secret")
    (root / "escape.txt").symlink_to(outside)
    for path in ("../secret.txt", ".env", "escape.txt", str(outside)):
        with pytest.raises(ValueError):
            cognition.index_document(path)
    (root / "large.txt").write_text("x" * 100001)
    with pytest.raises(ValueError):
        cognition.index_document("large.txt")
    (root / "binary.txt").write_bytes(b"a\0b")
    with pytest.raises(ValueError):
        cognition.index_document("binary.txt")
    (root / "safe.txt").write_text("safe content")
    cognition.index_document("safe.txt")
    (root / "safe.txt").unlink()
    (root / "safe.txt").symlink_to(outside)
    assert cognition.search("safe") == []


def test_progressive_skills_never_preload_body_or_escape_repo(store, project, tmp_path):
    skill = tmp_path / "skills" / "research"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Research\nPRIVATE_SKILL_BODY: read references carefully.\n")
    app = AgentApp(
        Config(project=project, workspace=tmp_path / "workspace", repo=tmp_path, demo=True),
        store=store,
        initialize=False,
    )
    assert "research" in app.agent().soul
    assert "PRIVATE_SKILL_BODY" not in app.agent().soul
    assert "PRIVATE_SKILL_BODY" in app.skills.load("research")["content"]
    with pytest.raises(ValueError):
        app.skills.load("../research")
    outside = tmp_path.parent / "outside-skill"
    outside.mkdir(exist_ok=True)
    (outside / "SKILL.md").write_text("SECRET")
    (tmp_path / "skills" / "escape").symlink_to(outside, target_is_directory=True)
    assert all(row["name"] != "escape" for row in SkillLibrary(tmp_path).list())
    with pytest.raises(ValueError):
        app.skills.load("escape")


def test_context_preserves_pairing_task_and_canonical_history():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "tool_calls": [{"id": "old-call"}]},
        {"role": "tool", "tool_call_id": "old-call", "content": "x" * 3000},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "current"},
        {"role": "assistant", "tool_calls": [{"id": "new-call"}]},
        {"role": "tool", "tool_call_id": "new-call", "content": "y" * 3000},
    ]
    fitted, metrics = fit_context(messages, [], 1200)
    assert request_size(fitted, []) <= 1200
    assert [m["content"] for m in fitted if m["role"] == "user"] == ["current"]
    assert fitted[-1]["tool_call_id"] == "new-call"
    assert len(messages[-1]["content"]) == 3000
    assert metrics["dropped_messages"] == 4 and metrics["shortened_results"] == 1
    with pytest.raises(ContextBudgetExceeded):
        fit_context(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "x" * 2000}], [], 500
        )


def test_repeat_detection_before_repeating_effects_and_metrics(store, project):
    executed = []

    class Model:
        def complete(self, messages, tools):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "same",
                        "type": "function",
                        "function": {"name": "effect", "arguments": '{"x":1}'},
                    }
                ],
            }

    tool = Tool(
        "effect",
        "effect",
        object_schema({"x": {"type": "integer"}}),
        lambda x: executed.append(x) or "ok",
    )
    agent = Agent(Model(), store, [tool], project=project, repeat_limit=2)
    with pytest.raises(RepeatedToolCall):
        agent.run("Execute")
    assert executed == [1, 1]
    assert agent.metrics["tool_calls"] == 2 and agent.metrics["model_calls"] == 3
    observation = store.observations(project)[0]
    assert observation["kind"] == "repeated_tool" and "agent.py" in observation["detail"]
    assert "Execute" not in observation["detail"] and not agent.history


def test_app_plan_tools_session_context_and_run_receipts(store, project, tmp_path):
    class Model:
        def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                return {"role": "assistant", "content": "Plan recorded"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "plan",
                        "type": "function",
                        "function": {
                            "name": "set_plan",
                            "arguments": json.dumps(
                                {"title": "Research", "steps": ["Read", "Write"]}
                            ),
                        },
                    }
                ],
            }

    app = AgentApp(
        Config(project=project, repo=tmp_path, workspace=tmp_path / "workspace"),
        model=Model(),
        store=store,
        initialize=False,
    )
    assert app.run("plan private topic", session="work") == "Plan recorded"
    assert app.cognition.plan("work")["steps"][0]["status"] == "todo"
    row = app.learning.list()[0]
    assert row["metrics"]["model_calls"] == 2 and row["metrics"]["tool_calls"] == 1
    assert "private topic" not in json.dumps(row)
    with pytest.raises(ValueError, match="AgentApp.run"):
        app.tools["read_plan"].invoke("{}")
    assert any(c["name"] == "knowledge" for c in app.capabilities())


def test_custom_tools_supply_evolvable_source_provenance(store, project, tmp_path):
    from evomesh import tool
    from evomesh.learning import capability_catalog

    @tool(source_paths=("company/retrieval.py",), capability="company-search")
    def company_search(query: str) -> str:
        """Search a company index."""
        raise ValueError("private diagnostic")

    class Model:
        count = 0

        def complete(self, messages, tools):
            self.count += 1
            return (
                {"role": "assistant", "content": "Handled"}
                if self.count > 1
                else {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "x",
                            "function": {
                                "name": "company_search",
                                "arguments": '{"query":"private query"}',
                            },
                        }
                    ],
                }
            )

    agent = Agent(Model(), store, [company_search], project=project)
    agent.run("private task")
    detail = json.loads(store.observations(project)[0]["detail"])
    assert detail["capability"] == "company-search"
    assert detail["source_paths"] == ["company/retrieval.py"]
    assert "private" not in json.dumps(detail)
    assert any(
        c["name"] == "tool:company_search" for c in capability_catalog(tmp_path, [company_search])
    )
