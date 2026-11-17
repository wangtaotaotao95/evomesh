import hashlib
import json
import sys

import pytest

from evomesh import AgentApp, Config
from evomesh.evaluation import compare_reports, evaluate


@pytest.mark.parametrize("written", [None, "WRONG_PRIVATE", "EXPECTED_PRIVATE\r\n"])
def test_file_assertions_check_artifact_even_when_model_claims_success(tmp_path, written):
    expected = "EXPECTED_PRIVATE\r\n"

    class Model:
        def complete(self, messages, tools):
            if written is None or messages[-1]["role"] == "tool":
                return {"role": "assistant", "content": "Saved successfully"}
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {
                                    "path": "PRIVATE_ARTIFACT.txt",
                                    "content": written,
                                }
                            ),
                        },
                    }
                ],
            }

    def factory():
        return AgentApp(
            Config(database_url="sqlite:///:memory:", workspace=tmp_path), model=Model()
        )

    suite = [
        {
            "name": "artifact",
            "prompt": "Save the synthetic artifact",
            "contains": ["Saved successfully"],
            "file_sha256": {"PRIVATE_ARTIFACT.txt": hashlib.sha256(expected.encode()).hexdigest()},
        }
    ]
    report = evaluate(factory, suite)
    assert report["cases"][0]["checks"]["contains"]
    assert report["passed"] is (written == expected)
    assert report["cases"][0]["checks"]["file_sha256"] is (written == expected)
    assert "PRIVATE_" not in json.dumps(report)


@pytest.mark.parametrize(
    "kind", ["binary", "inside-link", "outside-link", "directory-link", "directory", "oversized"]
)
def test_file_assertions_reject_symlinks_and_bound_binary_reads(tmp_path, kind):
    root = tmp_path / "workspace"
    root.mkdir()
    raw = b"\x00\xff\r\n"
    artifact = root / "output.bin"
    if kind == "binary":
        artifact.write_bytes(raw)
    elif kind in {"inside-link", "outside-link"}:
        target = (root if kind == "inside-link" else tmp_path) / "source.bin"
        target.write_bytes(raw)
        artifact.symlink_to(target)
    elif kind == "directory-link":
        target = tmp_path / "external"
        target.mkdir()
        (target / "data.bin").write_bytes(raw)
        artifact.symlink_to(target, target_is_directory=True)
    elif kind == "directory":
        artifact.mkdir()
    else:
        raw = b"x" * (10 * 1024 * 1024 + 1)
        artifact.write_bytes(raw)

    def factory():
        return AgentApp(Config(database_url="sqlite:///:memory:", workspace=root, demo=True))

    name = "output.bin/data.bin" if kind == "directory-link" else "output.bin"
    report = evaluate(
        factory,
        [
            {
                "name": "binary artifact",
                "prompt": "List files",
                "file_sha256": {name: hashlib.sha256(raw).hexdigest()},
            }
        ],
    )
    assert report["passed"] is (kind == "binary")


@pytest.mark.parametrize(
    "files",
    [
        {},
        [],
        {"../outside": "a" * 64},
        {"/absolute": "a" * 64},
        {".env": "a" * 64},
        {".git/config": "a" * 64},
        {"nul\x00path": "a" * 64},
        {"a": "invalid"},
        {"a": "A" * 64},
        {str(i): "a" * 64 for i in range(21)},
    ],
)
def test_invalid_file_assertions_fail_before_factory(files):
    def factory():
        pytest.fail("Invalid suite must be rejected before running an app")

    with pytest.raises(ValueError):
        evaluate(factory, [{"name": "invalid", "prompt": "p", "file_sha256": files}])


def test_file_expectations_participate_in_suite_fingerprint(tmp_path):
    def factory():
        return AgentApp(Config(database_url="sqlite:///:memory:", workspace=tmp_path, demo=True))

    suite = [{"name": "file", "prompt": "List", "file_sha256": {"missing": "a" * 64}}]
    baseline = evaluate(factory, suite)
    suite[0]["file_sha256"]["missing"] = "b" * 64
    with pytest.raises(ValueError, match="identical"):
        compare_reports(baseline, evaluate(factory, suite))


def test_artifact_evaluation_rejects_wrong_candidate_and_integrates_corrected_source(
    store, project, tmp_path
):
    from evomesh.evolution import Integrator, git, make_change, revision, submit

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "candidate.py").write_text("CONTENT = 'original'\n")
    (repo / "suite.json").write_text(
        json.dumps(
            [
                {
                    "name": "artifact",
                    "prompt": "Save fixture",
                    "contains": ["Saved"],
                    "file_sha256": {"result.txt": hashlib.sha256(b"correct").hexdigest()},
                }
            ]
        )
    )
    (repo / "fixture.py").write_text("""import json
import tempfile
from pathlib import Path
from contextlib import contextmanager
from evomesh import AgentApp, Config
from candidate import CONTENT
class Model:
    def complete(self, messages, tools):
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "Saved"}
        return {"role": "assistant", "tool_calls": [{
            "id": "write", "type": "function", "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "result.txt", "content": CONTENT})
            }
        }]}
@contextmanager
def create_app():
    with tempfile.TemporaryDirectory() as temp:
        config = Config(database_url="sqlite:///:memory:", workspace=Path(temp))
        with AgentApp(config, model=Model()) as app:
            yield app
""")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic evaluation gate")
    base = revision(repo)
    gate = Integrator(
        store,
        repo,
        project,
        [
            [
                sys.executable,
                "-m",
                "evomesh.cli",
                "evaluate",
                "suite.json",
                "--factory",
                "fixture:create_app",
            ]
        ],
    )
    for content, status in [("wrong", "rejected"), ("correct", "integrated")]:
        identifier = submit(
            store,
            repo,
            project=project,
            instance="fixture",
            target="code",
            rationale="Validate actual artifact",
            base=base,
            changes=[make_change(repo, base, "candidate.py", f"CONTENT = {content!r}\n")],
        )
        result = gate.once()
        assert result["id"] == identifier and result["status"] == status
        assert revision(repo) == base
        if status == "rejected":
            assert '"file_sha256": false' in result["report"]
            assert revision(repo, "evolution/main") == base
        else:
            assert '"file_sha256": true' in result["report"]
            assert revision(repo, "evolution/main") == result["commit"]


def test_evaluation_assertions_budgets_and_regression(tmp_path):
    def factory():
        return AgentApp(
            Config(
                database_url="sqlite:///:memory:",
                demo=True,
                workspace=tmp_path / "workspace",
                repo=tmp_path,
            )
        )

    suite = [
        {
            "name": "list",
            "prompt": "List files",
            "contains": ["Workspace files"],
            "required_tools": ["list_files"],
            "forbidden_tools": ["write_file"],
            "max_model_calls": 2,
            "max_tool_calls": 1,
            "max_tool_errors": 0,
        }
    ]
    report = evaluate(factory, suite)
    assert report["passed"]
    assert "List files" not in json.dumps(report)
    bad = json.loads(json.dumps(report))
    bad["cases"][0]["passed"] = False
    assert compare_reports(report, bad)["regressions"] == ["list"]
    with pytest.raises(ValueError):
        compare_reports(report, {"suite_sha256": "other"})
    suite[0]["max_model_calls"] = 1
    failed = evaluate(factory, suite)
    assert not failed["passed"] and not failed["cases"][0]["checks"]["max_model_calls"]


def test_evaluation_requires_assertions_and_redacts_exceptions():
    with pytest.raises(ValueError):
        evaluate(None, [{"name": "empty", "prompt": "hello"}])

    def broken():
        raise RuntimeError("private-key-in-error")

    result = evaluate(
        broken, [{"name": "broken", "prompt": "private prompt", "contains": ["answer"]}]
    )
    assert not result["passed"] and result["cases"][0]["error"] == "RuntimeError"
    assert "private" not in json.dumps(result)
    with pytest.raises(ValueError):
        evaluate(broken, [{"name": "x", "prompt": "p", "max_model_calls": True}])
