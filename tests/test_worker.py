"""Tests for the Phase B worker loop.

Covers the queue mechanic (B1 — pickup, recovery, sorted order), the
model-handoff output shape (B2 — diff.patch + status.txt), the git
push step (B3 — dirty-tree pre-flight + branch.txt), and the test
gate (B4 — tests_failed + failed/ routing + log.txt). The real model
call and real git subprocesses are both stubbed; see `test_git_ops.py`
for the git plumbing coverage.
"""

from __future__ import annotations

import json
import logging

import pytest

import git_ops
import job
import worker


@pytest.fixture
def jobs_root(tmp_path, monkeypatch):
    for sub in ("pending", "running", "done", "failed"):
        (tmp_path / sub).mkdir()
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path)
    # Stub git + gh by default — the queue+model tests don't exercise them.
    monkeypatch.setattr(git_ops, "is_clean", lambda _repo_root: True)
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **_: None)
    monkeypatch.setattr(git_ops, "create_draft_pr", lambda **_: "https://example/pull/stub")
    # Default group routing: no branch exists, no existing PR. Group-aware
    # tests override these.
    monkeypatch.setattr(git_ops, "branch_exists_on_origin", lambda _repo_root, _branch: False)
    monkeypatch.setattr(git_ops, "pr_url_for_branch", lambda _repo_root, _branch: None)
    return tmp_path


def _write_pending(
    jobs_root,
    jid: str,
    spec: str = "noop",
    scope=None,
    repo_root=None,
    skip_tests: bool = False,
    test_cmd=None,
    title: str | None = None,
    body: str | None = None,
    group_id: str | None = None,
):
    """Drop a well-formed job record into pending/."""
    rec = {
        "id": jid,
        "spec": spec,
        "scope": scope if scope is not None else [],
        "repo_root": repo_root or str(jobs_root),
        "base_branch": "main",
        "test_cmd": test_cmd if test_cmd is not None else ["uv", "run", "pytest", "-q"],
        "skip_tests": skip_tests,
        "title": title,
        "body": body,
        "group_id": group_id,
    }
    (jobs_root / "pending" / f"{jid}.json").write_text(json.dumps(rec))
    return rec


def _stub_transformation(monkeypatch, result: str = "") -> None:
    """Replace the model handoff with a canned return value."""
    monkeypatch.setattr(
        worker,
        "_run_transformation_sync",
        lambda spec, scope, repo_root: result,
    )


# ---------- B1 queue mechanic -------------------------------------------


def test_tick_empty_queue(jobs_root):
    assert worker.tick() == 0


def test_tick_picks_up_pending(jobs_root, monkeypatch):
    _stub_transformation(monkeypatch)
    _write_pending(jobs_root, "001")
    assert worker.tick() == 1
    assert (jobs_root / "done" / "001" / "job.json").exists()
    assert list((jobs_root / "pending").glob("*.json")) == []
    assert list((jobs_root / "running").glob("*.json")) == []


def test_tick_processes_in_sorted_order(jobs_root, monkeypatch, caplog):
    _stub_transformation(monkeypatch)
    caplog.set_level(logging.INFO, logger="rae-local-agent.worker")
    _write_pending(jobs_root, "003")
    _write_pending(jobs_root, "001")
    _write_pending(jobs_root, "002")
    assert worker.tick() == 3
    picked = [r.message for r in caplog.records if r.message.startswith("picked up ")]
    assert picked == ["picked up 001", "picked up 002", "picked up 003"]


def test_tick_recovers_orphaned_running(jobs_root, monkeypatch):
    """A job left in running/ by a crashed worker gets finished to done/<id>/."""
    _stub_transformation(monkeypatch)
    rec = {
        "id": "crashed",
        "spec": "s",
        "scope": [],
        "repo_root": str(jobs_root),
        "base_branch": "main",
        "test_cmd": ["true"],
        "skip_tests": True,
    }
    (jobs_root / "running" / "crashed.json").write_text(json.dumps(rec))
    assert worker.tick() == 1
    assert not (jobs_root / "running" / "crashed.json").exists()
    assert (jobs_root / "done" / "crashed" / "job.json").exists()


def test_recovery_precedes_pending(jobs_root, monkeypatch, caplog):
    _stub_transformation(monkeypatch)
    caplog.set_level(logging.INFO, logger="rae-local-agent.worker")
    orphan = {
        "id": "orphan",
        "spec": "s",
        "scope": [],
        "repo_root": str(jobs_root),
        "base_branch": "main",
        "test_cmd": ["true"],
        "skip_tests": True,
    }
    new = {
        "id": "new",
        "spec": "s",
        "scope": [],
        "repo_root": str(jobs_root),
        "base_branch": "main",
        "test_cmd": ["true"],
        "skip_tests": True,
    }
    (jobs_root / "running" / "orphan.json").write_text(json.dumps(orphan))
    (jobs_root / "pending" / "new.json").write_text(json.dumps(new))
    worker.tick()
    msgs = [r.message for r in caplog.records]
    assert "recovering orphan" in msgs
    assert "picked up new" in msgs
    assert msgs.index("recovering orphan") < msgs.index("picked up new")


# ---------- B2 outcome shape --------------------------------------------


def test_process_writes_ok_status_and_diff(jobs_root, monkeypatch):
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    _stub_transformation(monkeypatch, result=canned)
    _write_pending(jobs_root, "job-ok")
    worker.tick()

    out = jobs_root / "done" / "job-ok"
    assert (out / "job.json").exists()
    assert (out / "diff.patch").read_text() == canned
    assert (out / "status.txt").read_text() == "ok\n"


def test_process_writes_model_declined_on_empty_diff(jobs_root, monkeypatch):
    _stub_transformation(monkeypatch, result="")
    _write_pending(jobs_root, "job-decline")
    worker.tick()

    out = jobs_root / "done" / "job-decline"
    assert (out / "diff.patch").read_text() == ""
    assert (out / "status.txt").read_text() == "model_declined\n"
    assert not (out / "branch.txt").exists()


def test_process_writes_error_status_when_transformation_raises(jobs_root, monkeypatch):
    def boom(spec, scope, repo_root):
        raise ValueError("repo_root is not a directory: /nope")

    monkeypatch.setattr(worker, "_run_transformation_sync", boom)
    _write_pending(jobs_root, "job-err")
    worker.tick()

    out = jobs_root / "done" / "job-err"
    status = (out / "status.txt").read_text()
    assert status.startswith("error:ValueError:")
    assert "repo_root is not a directory" in status
    assert (out / "job.json").exists()
    assert (out / "diff.patch").read_text() == ""
    assert not (out / "branch.txt").exists()


# ---------- B3 git push -------------------------------------------------


def test_process_writes_branch_txt_on_successful_push(jobs_root, monkeypatch):
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    _stub_transformation(monkeypatch, result=canned)
    _write_pending(jobs_root, "job-push")
    worker.tick()

    out = jobs_root / "done" / "job-push"
    assert (out / "status.txt").read_text() == "ok\n"
    assert (out / "branch.txt").read_text() == "rae-local-agent/job-push\n"


def test_process_passes_record_fields_through_to_git_ops(jobs_root, monkeypatch):
    """The record's base_branch, repo_root, test_cmd, spec reach git_ops."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    seen: dict = {}
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **kw: seen.update(kw))

    _write_pending(
        jobs_root,
        "job-fields",
        spec="rename foo",
        repo_root=str(jobs_root),
        test_cmd=["make", "test"],
    )
    worker.tick()

    assert seen["base_branch"] == "main"
    assert str(seen["repo_root"]) == str(jobs_root)
    assert seen["new_branch"] == "rae-local-agent/job-fields"
    # No explicit title/body on the record → both fall back to the spec.
    assert seen["commit_message"] == "rename foo\n\nrename foo"
    assert seen["diff_text"] == canned
    assert seen["test_cmd"] == ["make", "test"]


def test_process_refuses_dirty_tree_without_calling_model(jobs_root, monkeypatch):
    monkeypatch.setattr(git_ops, "is_clean", lambda _repo_root: False)

    calls = []

    def record_call(spec, scope, repo_root):
        calls.append((spec, scope, repo_root))
        return "doesnt matter"

    monkeypatch.setattr(worker, "_run_transformation_sync", record_call)
    _write_pending(jobs_root, "job-dirty")
    worker.tick()

    out = jobs_root / "done" / "job-dirty"
    assert (out / "status.txt").read_text() == "error:dirty_tree\n"
    assert (out / "diff.patch").read_text() == ""
    assert not (out / "branch.txt").exists()
    assert calls == []


def test_process_writes_error_status_on_push_failure(jobs_root, monkeypatch):
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)

    def boom(**_):
        raise git_ops.GitError("git push failed: remote hung up")

    monkeypatch.setattr(git_ops, "branch_and_push", boom)
    _write_pending(jobs_root, "job-pushfail")
    worker.tick()

    out = jobs_root / "done" / "job-pushfail"
    status = (out / "status.txt").read_text()
    assert status.startswith("error:GitError:")
    assert "remote hung up" in status
    assert not (out / "branch.txt").exists()
    assert (out / "diff.patch").read_text() == ""


# ---------- B4 test gate ------------------------------------------------


def test_process_routes_to_failed_dir_on_tests_failed(jobs_root, monkeypatch):
    """TestsFailedError → status `tests_failed`, record lands in failed/<id>/."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    _stub_transformation(monkeypatch, result=canned)

    def tests_failed(**_):
        raise git_ops.TestsFailedError("FAILED tests/test_foo.py\n1 failed in 0.3s\n")

    monkeypatch.setattr(git_ops, "branch_and_push", tests_failed)
    _write_pending(jobs_root, "job-tests-fail")
    worker.tick()

    out = jobs_root / "failed" / "job-tests-fail"
    assert (out / "job.json").exists()
    assert (out / "status.txt").read_text() == "tests_failed\n"
    assert (out / "diff.patch").read_text() == canned  # diff retained for debugging
    assert "1 failed in 0.3s" in (out / "log.txt").read_text()
    assert not (out / "branch.txt").exists()
    # The done/ dir should NOT have been touched.
    assert not (jobs_root / "done" / "job-tests-fail").exists()


def test_process_passes_none_test_cmd_when_skip_tests_true(jobs_root, monkeypatch):
    """skip_tests=True → test_cmd arrives at git_ops as None."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    seen: dict = {}
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **kw: seen.update(kw))

    _write_pending(jobs_root, "job-skip", skip_tests=True)
    worker.tick()

    assert seen["test_cmd"] is None


def test_process_passes_default_test_cmd_when_skip_tests_false(jobs_root, monkeypatch):
    """Default record (skip_tests=False) → test_cmd is forwarded as the list."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    seen: dict = {}
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **kw: seen.update(kw))

    _write_pending(jobs_root, "job-with-tests")
    worker.tick()

    assert seen["test_cmd"] == ["uv", "run", "pytest", "-q"]


# ---------- B5 draft PR creation ----------------------------------------


def test_process_writes_pr_url_on_successful_pr(jobs_root, monkeypatch):
    """On `ok`, pr_url.txt holds the URL returned by create_draft_pr."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    monkeypatch.setattr(
        git_ops,
        "create_draft_pr",
        lambda **_: "https://github.com/u/r/pull/7",
    )
    _write_pending(jobs_root, "job-pr-ok")
    worker.tick()

    out = jobs_root / "done" / "job-pr-ok"
    assert (out / "status.txt").read_text() == "ok\n"
    assert (out / "branch.txt").read_text() == "rae-local-agent/job-pr-ok\n"
    assert (out / "pr_url.txt").read_text() == "https://github.com/u/r/pull/7\n"


def test_process_passes_record_fields_through_to_create_draft_pr(jobs_root, monkeypatch):
    """repo_root, branch, title, body reach create_draft_pr."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    seen: dict = {}

    def fake_pr(**kw):
        seen.update(kw)
        return "https://example/pr"

    monkeypatch.setattr(git_ops, "create_draft_pr", fake_pr)
    _write_pending(jobs_root, "job-pr-fields", spec="rename foo", repo_root=str(jobs_root))
    worker.tick()

    assert str(seen["repo_root"]) == str(jobs_root)
    assert seen["branch"] == "rae-local-agent/job-pr-fields"
    # No explicit title/body on the record → fall back to the spec.
    assert seen["title"] == "rename foo"
    assert seen["body"] == "rename foo"


# ---------- Title/body overrides on the enqueue surface -------------------


def test_process_forwards_explicit_title_and_body_verbatim(jobs_root, monkeypatch):
    """Caller-supplied title/body reach create_draft_pr unmodified."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    pr_seen: dict = {}
    push_seen: dict = {}
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **kw: push_seen.update(kw))
    monkeypatch.setattr(
        git_ops,
        "create_draft_pr",
        lambda **kw: pr_seen.update(kw) or "https://example/pr",
    )

    long_spec = (
        "Add a return type annotation `-> ResponseReturnValue` to every "
        "function in this file decorated with the blueprint route."
    )
    _write_pending(
        jobs_root,
        "job-explicit",
        spec=long_spec,
        title="feat: annotate route return types",
        body="Context paragraph for the reviewer.",
    )
    worker.tick()

    assert pr_seen["title"] == "feat: annotate route return types"
    assert pr_seen["body"] == "Context paragraph for the reviewer."
    assert (
        push_seen["commit_message"]
        == "feat: annotate route return types\n\nContext paragraph for the reviewer."
    )


def test_process_falls_back_to_derived_title_and_full_spec_body(jobs_root, monkeypatch):
    """Omitted title/body: title summarizes the spec (truncated), body is the full spec."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    pr_seen: dict = {}
    push_seen: dict = {}
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **kw: push_seen.update(kw))
    monkeypatch.setattr(
        git_ops,
        "create_draft_pr",
        lambda **kw: pr_seen.update(kw) or "https://example/pr",
    )

    long_spec = (
        "Add a return type annotation `-> ResponseReturnValue` to every "
        "function in this file decorated with the blueprint route."
    )
    _write_pending(jobs_root, "job-fallback", spec=long_spec)
    worker.tick()

    # Title is truncated at the module cap, body is the untouched spec.
    assert pr_seen["title"] == git_ops._summarize(long_spec)
    assert len(pr_seen["title"]) <= git_ops.MAX_TITLE_LEN
    assert pr_seen["title"].endswith("…")
    assert pr_seen["body"] == long_spec
    assert push_seen["commit_message"] == f"{pr_seen['title']}\n\n{long_spec}"


def test_process_errors_but_preserves_branch_when_pr_fails_after_push(jobs_root, monkeypatch):
    """gh fails after branch is pushed → status=error, branch.txt retained, no pr_url.txt."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)

    def boom(**_):
        raise git_ops.GhError("gh pr create failed: not authenticated")

    monkeypatch.setattr(git_ops, "create_draft_pr", boom)
    _write_pending(jobs_root, "job-pr-fail")
    worker.tick()

    out = jobs_root / "done" / "job-pr-fail"
    status = (out / "status.txt").read_text()
    assert status.startswith("error:GhError:")
    assert "not authenticated" in status
    # Branch IS pushed, so branch.txt is retained for manual-PR recovery.
    assert (out / "branch.txt").read_text() == "rae-local-agent/job-pr-fail\n"
    assert not (out / "pr_url.txt").exists()


# ---------- B9 group_id routing -----------------------------------------


def test_first_in_group_uses_group_branch_name(jobs_root, monkeypatch):
    """First job in a group: branch is `rae-local-agent/<group_id>`, PR opens."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    push_seen: dict = {}
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **kw: push_seen.update(kw))
    # No branch yet — first in group.
    monkeypatch.setattr(git_ops, "branch_exists_on_origin", lambda _r, _b: False)
    pr_seen: dict = {}
    monkeypatch.setattr(
        git_ops,
        "create_draft_pr",
        lambda **kw: pr_seen.update(kw) or "https://example/pull/42",
    )

    _write_pending(jobs_root, "first-in-group", group_id="routes-sweep")
    worker.tick()

    assert push_seen["new_branch"] == "rae-local-agent/routes-sweep"
    # First-in-group bases off the record's base_branch, not the group branch.
    assert push_seen["base_branch"] == "main"
    assert pr_seen["branch"] == "rae-local-agent/routes-sweep"

    out = jobs_root / "done" / "first-in-group"
    assert (out / "status.txt").read_text() == "ok\n"
    assert (out / "branch.txt").read_text() == "rae-local-agent/routes-sweep\n"
    assert (out / "pr_url.txt").read_text() == "https://example/pull/42\n"


def test_later_in_group_appends_to_existing_branch_and_reuses_pr(jobs_root, monkeypatch):
    """Later-in-group: base off the group branch, reuse the open PR URL."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    push_seen: dict = {}
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **kw: push_seen.update(kw))
    # Branch already there → later-in-group path.
    monkeypatch.setattr(git_ops, "branch_exists_on_origin", lambda _r, _b: True)
    monkeypatch.setattr(git_ops, "pr_url_for_branch", lambda _r, _b: "https://example/pull/99")
    # If this is called, the test fails loudly — later-in-group must NOT
    # try to open a fresh PR when one already exists.
    monkeypatch.setattr(
        git_ops,
        "create_draft_pr",
        lambda **_: pytest.fail("create_draft_pr must not be called for later-in-group"),
    )

    _write_pending(jobs_root, "second-in-group", group_id="routes-sweep")
    worker.tick()

    assert push_seen["new_branch"] == "rae-local-agent/routes-sweep"
    # Later-in-group bases the new commit on the existing branch, not `main`.
    assert push_seen["base_branch"] == "rae-local-agent/routes-sweep"

    out = jobs_root / "done" / "second-in-group"
    assert (out / "status.txt").read_text() == "ok\n"
    assert (out / "pr_url.txt").read_text() == "https://example/pull/99\n"


def test_later_in_group_falls_back_to_create_pr_when_none_found(jobs_root, monkeypatch):
    """Branch exists but gh can't find a PR (closed/deleted): open a fresh one."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    monkeypatch.setattr(git_ops, "branch_exists_on_origin", lambda _r, _b: True)
    monkeypatch.setattr(git_ops, "pr_url_for_branch", lambda _r, _b: None)
    pr_seen: dict = {}
    monkeypatch.setattr(
        git_ops,
        "create_draft_pr",
        lambda **kw: pr_seen.update(kw) or "https://example/pull/recover",
    )

    _write_pending(jobs_root, "recover-group", group_id="routes-sweep")
    worker.tick()

    assert pr_seen["branch"] == "rae-local-agent/routes-sweep"

    out = jobs_root / "done" / "recover-group"
    assert (out / "status.txt").read_text() == "ok\n"
    assert (out / "pr_url.txt").read_text() == "https://example/pull/recover\n"


def test_ungrouped_job_keeps_per_job_branch_name(jobs_root, monkeypatch):
    """No group_id → branch is `rae-local-agent/<job-id>`, same as before B9."""
    canned = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    _stub_transformation(monkeypatch, result=canned)
    push_seen: dict = {}
    monkeypatch.setattr(git_ops, "branch_and_push", lambda **kw: push_seen.update(kw))

    # Should NOT call the probe when group_id is absent.
    monkeypatch.setattr(
        git_ops,
        "branch_exists_on_origin",
        lambda _r, _b: pytest.fail("branch_exists_on_origin called for an ungrouped job"),
    )

    _write_pending(jobs_root, "classic-job")  # group_id defaults to None
    worker.tick()

    assert push_seen["new_branch"] == "rae-local-agent/classic-job"
    assert push_seen["base_branch"] == "main"
