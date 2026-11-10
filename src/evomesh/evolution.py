"""Whole-file proposals, isolated validation, and atomic Git integration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath

from .store import LeaseLost, Store


class Conflict(ValueError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=EvoMesh",
            "-c",
            "user.email=evomesh@localhost",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "Git operation failed")
    return result.stdout.strip()


def safe_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or str(path) != name
        or any(p.startswith(".git") for p in path.parts)
        or any(p == ".env" or p.startswith(".env.") for p in path.parts)
    ):
        raise ValueError(f"Invalid repository path: {name!r}")
    return path


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def revision(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def source_file(repo: Path, commit: str, path: str) -> bytes | None:
    safe_path(path)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("Expected a full commit SHA")
    entry = git(repo, "ls-tree", commit, "--", path)
    if not entry:
        return None
    if not entry.startswith(("100644 blob ", "100755 blob ")):
        raise ValueError("Only regular text files can evolve")
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"], capture_output=True, check=True
    ).stdout


def make_change(repo: Path, commit: str, path: str, content: str | None) -> dict:
    old = source_file(repo, commit, path)
    return {
        "path": path,
        "before_sha256": sha(old) if old is not None else None,
        "content": content,
    }


def make_edit_change(repo: Path, commit: str, path: str, edits: list[dict]) -> dict:
    """Expand sequential, unique literal replacements against a pinned Git blob.

    No checkout files are written. The result uses the same full-content/hash
    contract as make_change, so SQL storage and integration gates stay identical.
    """
    if not isinstance(edits, list) or not 1 <= len(edits) <= 64:
        raise ValueError("Provide 1–64 exact edits")
    old = source_file(repo, commit, path)
    if old is None:
        raise ValueError("Exact edits require an existing source file")
    if len(old) > 100_000:
        raise ValueError("Source file exceeds 100 KB")
    content = old.decode("utf-8")
    if "\0" in content:
        raise ValueError("Only UTF-8 text sources are supported")
    for edit in edits:
        if (
            not isinstance(edit, dict)
            or set(edit) != {"old", "new"}
            or not isinstance(edit["old"], str)
            or not edit["old"]
            or not isinstance(edit["new"], str)
            or "\0" in edit["old"]
            or "\0" in edit["new"]
        ):
            raise ValueError("Each edit requires non-empty old and text new")
        first = content.find(edit["old"])
        if first == -1 or content.find(edit["old"], first + 1) != -1:
            raise ValueError("Expected exactly one match; read more source for unique context")
        content = content.replace(edit["old"], edit["new"], 1)
        if len(content.encode("utf-8")) > 100_000:
            raise ValueError("Edited source exceeds 100 KB")
    if content.encode("utf-8") == old:
        raise ValueError("Edits must change source")
    return {"path": path, "before_sha256": sha(old), "content": content}


def validate_changes(repo: Path, base: str, changes: list[dict]) -> None:
    revision(repo, base)
    seen = set()
    for change in changes:
        if set(change) != {"path", "before_sha256", "content"}:
            raise ValueError("Each change requires path, before_sha256 and content")
        path = change["path"]
        safe_path(path)
        if path in seen:
            raise ValueError("Duplicate path")
        seen.add(path)
        old = source_file(repo, base, path)
        expected = sha(old) if old is not None else None
        if expected != change["before_sha256"]:
            raise ValueError(f"Base hash mismatch: {path}")
        content = change["content"]
        if content is not None and (not isinstance(content, str) or "\0" in content):
            raise ValueError("Only UTF-8 text changes are supported")
        if (content.encode() if content is not None else None) == old:
            raise ValueError(f"No change: {path}")


def submit(
    store: Store,
    repo: Path,
    *,
    project: str,
    instance: str,
    target: str,
    rationale: str,
    changes: list[dict],
    base: str | None = None,
    observation_ids: list[str] | None = None,
) -> str:
    base = revision(repo, base or "HEAD")
    validate_changes(repo, base, changes)
    return store.submit(
        project=project,
        instance=instance,
        target=target,
        base_commit=base,
        rationale=rationale,
        changes=changes,
        observation_ids=observation_ids,
    )


class Integrator:
    """Integrate into a dedicated, unchecked-out local branch.

    Workers must share one Git object store/ref authority as well as one DB.
    Separate agent checkouts may submit to the same DB. Remote publication and
    deployment are explicit operator steps, outside the Git/SQL transaction.
    """

    def __init__(
        self,
        store: Store,
        repo: Path,
        project: str,
        checks: list[list[str]],
        branch: str = "evolution/main",
        timeout: float = 300,
        lease_seconds: float = 120,
    ):
        if not checks or any(not check for check in checks):
            raise ValueError("At least one validation command is required")
        if timeout <= 0 or lease_seconds < 3:
            raise ValueError("Positive timeout and a lease of at least 3 seconds are required")
        self.store, self.repo, self.project = store, repo.resolve(), project
        self.checks, self.branch = checks, branch
        self.timeout, self.lease_seconds = timeout, lease_seconds
        git(self.repo, "check-ref-format", f"refs/heads/{branch}")

    def _head(self) -> str:
        ref = f"refs/heads/{self.branch}"
        if f"branch {ref}\n" in git(self.repo, "worktree", "list", "--porcelain") + "\n":
            raise ValueError("Integration branch must not be checked out in any worktree")
        exists = git(self.repo, "show-ref", "--verify", "--hash", ref, check=False)
        if not exists:
            # Creating a ref is also CAS: another worker may initialize first.
            git(self.repo, "update-ref", ref, revision(self.repo), "0" * 40, check=False)
        return revision(self.repo, ref)

    def _previous(self, head: str, identifier: str) -> str | None:
        marker = f"EvoMesh-Proposal: {identifier}"
        log = git(
            self.repo, "log", head, "--format=%H%x00%B%x00", "--fixed-strings", f"--grep={marker}"
        )
        parts = log.split("\0")
        for index in range(0, len(parts) - 1, 2):
            if marker in parts[index + 1].splitlines():
                return parts[index].strip()
        return None

    def _validate(self, work: Path) -> str:
        reports = []
        # Candidate code must not inherit API/DB credentials. This is environment
        # hygiene, not an OS sandbox; use a disposable runner for untrusted code.
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR"}
        }
        with tempfile.TemporaryDirectory(prefix="evomesh-check-home-") as home:
            env.update(
                HOME=home,
                PYTHONPATH=str(work / "src") + os.pathsep + str(work),
                PYTHONNOUSERSITE="1",
                PYTHONDONTWRITEBYTECODE="1",
            )
            for command in self.checks:
                with tempfile.TemporaryFile() as output:
                    process = subprocess.Popen(
                        command,
                        cwd=work,
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    try:
                        code = process.wait(timeout=self.timeout)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        raise ValueError(f"Validation timed out: {command[0]}") from None
                    output.seek(0, os.SEEK_END)
                    output.seek(max(0, output.tell() - 8000))
                    report = output.read().decode(errors="replace")
                reports.append(f"{command!r}\n{report}")
                if code:
                    raise ValueError("Validation failed\n" + "\n".join(reports))
        return "\n".join(reports)

    def once(self) -> dict | None:
        head = self._head()
        proposal = self.store.claim(self.project, self.lease_seconds)
        if not proposal:
            return None
        identifier, token = proposal["id"], proposal["token"]
        stop, lost = threading.Event(), threading.Event()

        def heartbeat():
            while not stop.wait(self.lease_seconds / 3):
                try:
                    self.store.renew(identifier, token, self.lease_seconds)
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        status, report, commit = "error", "", None
        try:
            previous = self._previous(head, identifier)
            if previous:
                status, commit, report = "integrated", previous, "Recovered published commit"
            else:
                changes = json.loads(proposal["changes"])
                validate_changes(self.repo, proposal["base_commit"], changes)
                # Bases must belong to this branch's ancestry.
                merge_base = git(self.repo, "merge-base", head, proposal["base_commit"])
                if merge_base != proposal["base_commit"]:
                    raise Conflict("Proposal base is not an ancestor of the integration head")
                with tempfile.TemporaryDirectory(prefix="evomesh-integrate-") as temp:
                    work = (Path(temp) / "candidate").resolve()
                    git(self.repo, "worktree", "add", "--detach", str(work), head)
                    try:
                        for change in changes:
                            name = change["path"]
                            old = source_file(self.repo, head, name)
                            if (sha(old) if old is not None else None) != change["before_sha256"]:
                                raise Conflict(f"Changed since proposal base: {name}")
                            path = work / name
                            if not path.resolve().is_relative_to(work.resolve()):
                                raise ValueError("Symlink escape")
                            # Reject any symlink ancestor, even an internal one.
                            if any(
                                p.is_symlink()
                                for p in [path, *path.parents]
                                if p.is_relative_to(work) and p != work
                            ):
                                raise ValueError("Symlink path")
                            if change["content"] is None:
                                path.unlink()
                            else:
                                path.parent.mkdir(parents=True, exist_ok=True)
                                path.write_text(change["content"], encoding="utf-8")
                        paths = [c["path"] for c in changes]
                        git(work, "add", "--", *paths)
                        tree = git(work, "write-tree")
                        report = self._validate(work)
                        git(work, "add", "-u")
                        if git(work, "write-tree") != tree:
                            raise ValueError("Validation modified tracked candidate files")
                        if lost.is_set():
                            raise LeaseLost(identifier)
                        self.store.renew(identifier, token, self.lease_seconds)
                        # commit-tree commits exactly the validated index; no hooks execute.
                        commit = git(
                            work,
                            "commit-tree",
                            tree,
                            "-p",
                            head,
                            "-m",
                            f"evolve({proposal['target']}): {identifier}\n\n"
                            f"EvoMesh-Proposal: {identifier}",
                        )
                        if self._head() != head:
                            status, commit, report = "pending", None, "Head moved; retry validation"
                        else:
                            try:
                                git(
                                    self.repo,
                                    "update-ref",
                                    f"refs/heads/{self.branch}",
                                    commit,
                                    head,
                                )
                                status = "integrated"
                            except RuntimeError:
                                status, commit, report = (
                                    "pending",
                                    None,
                                    "Ref raced; retry validation",
                                )
                    finally:
                        git(self.repo, "worktree", "remove", "--force", str(work), check=False)
        except LeaseLost:
            raise
        except Conflict as exc:
            status, report = "conflict", str(exc)
        except (ValueError, TypeError, KeyError) as exc:
            status, report = "rejected", str(exc)
        except Exception as exc:
            status, report = "error", f"{type(exc).__name__}: {exc}"
        finally:
            stop.set()
            thread.join()
        self.store.finish(identifier, token, status, report, commit)
        return {"id": identifier, "status": status, "commit": commit, "report": report}
