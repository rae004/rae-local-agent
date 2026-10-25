"""Tests for the Phase C issue poller.

Covers:

- `main()` exit codes (valid / missing / invalid config).
- `tick()` shape: both passes fire, per-repo failures don't take out
  sibling repos.
- `_handle_issue` paths: accept (label-first, write pending, comment),
  reject on parse error (comment + agent-invalid label), reject on
  enqueue error.
- Completion pass (`_handle_completion`): happy path, still-running,
  no-tag, no-record, comment already there (idempotency), partial
  write failures.
- Comment formatter per status (ok / tests_failed / model_declined /
  error:...).
- Signal handling + interruptible sleep.

Each `main()` test monkeypatches `signal.signal` to a no-op so we
don't leave global SIGINT/SIGTERM handlers behind for the rest of the
suite.
"""

from __future__ import annotations

import logging
import signal
from pathlib import Path
from textwrap import dedent

import pytest

import config
import gh_api
import issue_poller
import job


@pytest.fixture(autouse=True)
def _reset_stop_flag():
    """Keep the module-level `_flag` from leaking state between tests."""
    issue_poller._flag.stopped = False
    yield
    issue_poller._flag.stopped = False


@pytest.fixture
def _jobs_dir(tmp_path, monkeypatch):
    """Point the `job` module at a tmp root so write_pending doesn't touch the real queue."""
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path / "jobs")
    (tmp_path / "jobs" / "pending").mkdir(parents=True)
    return tmp_path / "jobs"


@pytest.fixture
def _no_completion_pass(monkeypatch):
    """Default: completion pass sees no issues. Tests that care override."""
    monkeypatch.setattr(gh_api, "list_issues_awaiting_completion", lambda slug: [])


def _write_valid_config(tmp_path: Path, poll_interval: int = 60) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    cfg_path = tmp_path / "repos.yaml"
    cfg_path.write_text(
        f"poll_interval_seconds: {poll_interval}\nrepos:\n  owner/r: {repo}\n",
        encoding="utf-8",
    )
    return cfg_path


def _good_body(tmp_path: Path) -> str:
    """A representative issue body that parses + validates cleanly."""
    return dedent(
        """
        ---
        scope:
          - a.py
        title: "chore: do it"
        ---

        Do the thing.
        """
    ).lstrip()


# ---------- main() exit codes -------------------------------------------


def test_main_returns_zero_on_valid_config(tmp_path, monkeypatch, caplog):
    cfg_path = _write_valid_config(tmp_path)
    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(issue_poller.signal, "signal", lambda *a, **k: None)

    def _mock_tick(_cfg):
        issue_poller._flag.stopped = True
        return 0

    monkeypatch.setattr(issue_poller, "tick", _mock_tick)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    assert issue_poller.main() == 0
    assert any("poller up" in r.message for r in caplog.records)
    assert any("poller down" in r.message for r in caplog.records)


def test_main_returns_one_on_missing_config(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", missing)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", missing)
    caplog.set_level(logging.ERROR, logger="rae-local-agent.issue_poller")

    assert issue_poller.main() == 1
    assert any("config file not found" in r.message for r in caplog.records)


def test_main_returns_one_on_invalid_config(tmp_path, monkeypatch, caplog):
    cfg_path = tmp_path / "repos.yaml"
    cfg_path.write_text("poll_interval_seconds: 60\n", encoding="utf-8")
    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", cfg_path)
    caplog.set_level(logging.ERROR, logger="rae-local-agent.issue_poller")

    assert issue_poller.main() == 1
    assert any("`repos` field is required" in r.message for r in caplog.records)


def test_main_installs_signal_handlers_only_after_config_is_valid(tmp_path, monkeypatch):
    installed: list = []

    def _record(signum, handler):
        installed.append((signum, handler))

    monkeypatch.setattr(issue_poller, "DEFAULT_CONFIG_PATH", tmp_path / "nope.yaml")
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", tmp_path / "nope.yaml")
    monkeypatch.setattr(issue_poller.signal, "signal", _record)

    assert issue_poller.main() == 1
    assert installed == []


# ---------- tick() shape ------------------------------------------------


def _cfg_with_repos(*slugs: str) -> config.Config:
    return config.Config(
        poll_interval_seconds=60,
        repos={
            slug: config.RepoConfig(path=Path(f"/tmp/{slug.replace('/', '_')}")) for slug in slugs
        },
    )


def test_tick_calls_handle_issue_for_each(monkeypatch, caplog, _no_completion_pass):
    """One issue in; `_handle_issue` fired exactly once with the right args."""
    issues = [
        gh_api.Issue(number=1, title="first", url="u1", body="b1"),
        gh_api.Issue(number=2, title="second", url="u2", body="b2"),
    ]
    monkeypatch.setattr(gh_api, "list_pollable_issues", lambda slug: issues)
    seen: list = []
    monkeypatch.setattr(
        issue_poller,
        "_handle_issue",
        lambda slug, issue, repo_cfg: seen.append((slug, issue.number, str(repo_cfg.path))),
    )

    n = issue_poller.tick(_cfg_with_repos("owner/repo"))

    assert n == 2
    assert seen == [
        ("owner/repo", 1, "/tmp/owner_repo"),
        ("owner/repo", 2, "/tmp/owner_repo"),
    ]


def test_tick_iterates_all_repos(monkeypatch, _no_completion_pass):
    seen: list[str] = []
    monkeypatch.setattr(
        gh_api,
        "list_pollable_issues",
        lambda slug: (seen.append(slug), [])[1],
    )
    monkeypatch.setattr(issue_poller, "_handle_issue", lambda *a: None)

    issue_poller.tick(_cfg_with_repos("owner/a", "owner/b", "owner/c"))

    assert seen == ["owner/a", "owner/b", "owner/c"]


def test_tick_skips_repo_on_gh_error_and_continues(monkeypatch, caplog, _no_completion_pass):
    """Repo B shouldn't go dark just because repo A's token lapsed."""

    def _list(slug):
        if slug == "owner/a":
            raise gh_api.GhCliError("auth required")
        return [gh_api.Issue(number=3, title="third", url="u3", body="body")]

    monkeypatch.setattr(gh_api, "list_pollable_issues", _list)
    monkeypatch.setattr(issue_poller, "_handle_issue", lambda *a: None)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller.tick(_cfg_with_repos("owner/a", "owner/b"))

    msgs = [r.message for r in caplog.records]
    assert any("skipping owner/a" in m for m in msgs)


def test_tick_runs_both_passes_per_repo(monkeypatch):
    """Each repo gets an adopt list AND a completion list, in that order."""
    seen: list[tuple[str, str]] = []

    def _list_adopt(slug):
        seen.append(("adopt", slug))
        return []

    def _list_complete(slug):
        seen.append(("complete", slug))
        return []

    monkeypatch.setattr(gh_api, "list_pollable_issues", _list_adopt)
    monkeypatch.setattr(gh_api, "list_issues_awaiting_completion", _list_complete)

    issue_poller.tick(_cfg_with_repos("owner/a", "owner/b"))

    assert seen == [
        ("adopt", "owner/a"),
        ("complete", "owner/a"),
        ("adopt", "owner/b"),
        ("complete", "owner/b"),
    ]


def test_tick_completion_gh_error_skips_but_adopt_still_ran(monkeypatch, caplog):
    """A completion-pass gh error must NOT prevent the next repo's adopt."""
    adopt_seen: list[str] = []

    def _list_adopt(slug):
        adopt_seen.append(slug)
        return []

    def _list_complete(slug):
        if slug == "owner/a":
            raise gh_api.GhCliError("completion lapse")
        return []

    monkeypatch.setattr(gh_api, "list_pollable_issues", _list_adopt)
    monkeypatch.setattr(gh_api, "list_issues_awaiting_completion", _list_complete)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller.tick(_cfg_with_repos("owner/a", "owner/b"))

    assert adopt_seen == ["owner/a", "owner/b"]
    msgs = [r.message for r in caplog.records]
    assert any("skipping owner/a this tick (completion)" in m for m in msgs)


# ---------- _handle_issue: accept path ---------------------------------


def test_handle_issue_accept_uses_repo_gate_cmds_when_issue_did_not_specify(
    tmp_path, monkeypatch, _jobs_dir
):
    """Repo-level default flows into the record when the issue body is silent."""
    body = _good_body(tmp_path)
    issue = gh_api.Issue(number=7, title="t", url="u", body=body)
    repo_cfg = config.RepoConfig(
        path=tmp_path,
        gate_cmds=(("npm", "run", "build"),),
    )
    monkeypatch.setattr(gh_api, "add_label", lambda *a: None)
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda *a: None)

    issue_poller._handle_issue("owner/repo", issue, repo_cfg)

    pending = list((_jobs_dir / "pending").iterdir())
    assert len(pending) == 1
    import json as _json

    rec = _json.loads(pending[0].read_text())
    assert rec["gate_cmds"] == [["npm", "run", "build"]]


def test_handle_issue_accept_issue_gate_cmds_override_repo_default(
    tmp_path, monkeypatch, _jobs_dir
):
    """Issue frontmatter `gate_cmds` wins over the repo default."""
    body = dedent(
        """
        ---
        scope:
          - a.py
        gate_cmds:
          - [pytest, -x]
        ---

        Do it.
        """
    ).lstrip()
    issue = gh_api.Issue(number=7, title="t", url="u", body=body)
    repo_cfg = config.RepoConfig(
        path=tmp_path,
        gate_cmds=(("npm", "run", "build"),),  # should be ignored
    )
    monkeypatch.setattr(gh_api, "add_label", lambda *a: None)
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda *a: None)

    issue_poller._handle_issue("owner/repo", issue, repo_cfg)

    pending = list((_jobs_dir / "pending").iterdir())
    assert len(pending) == 1
    import json as _json

    rec = _json.loads(pending[0].read_text())
    assert rec["gate_cmds"] == [["pytest", "-x"]]


def test_handle_issue_accept_writes_label_before_comment(tmp_path, monkeypatch, _jobs_dir, caplog):
    """Label must land before the comment. (`_make_job_record` runs first but
    is local-only — only the two gh writes have observable ordering.)"""
    body = _good_body(tmp_path)
    issue = gh_api.Issue(number=7, title="t", url="u", body=body)
    calls: list[tuple] = []

    def _rec_label(slug, number, label):
        calls.append(("label", slug, number, label))

    def _rec_comment(slug, number, body):
        calls.append(("comment", slug, number, body))

    monkeypatch.setattr(gh_api, "add_label", _rec_label)
    monkeypatch.setattr(gh_api, "comment_on_issue", _rec_comment)
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, config.RepoConfig(path=tmp_path))

    kinds = [c[0] for c in calls]
    assert kinds == ["label", "comment"]
    _, _, _, applied_label = calls[0]
    assert applied_label == issue_poller.LABEL_QUEUED

    # A pending job landed on disk — one and only one.
    pending = list((_jobs_dir / "pending").iterdir())
    assert len(pending) == 1
    import json as _json

    rec = _json.loads(pending[0].read_text())
    assert rec["spec"] == "Do the thing."
    assert rec["scope"] == ["a.py"]
    assert rec["title"] == "chore: do it"
    assert str(tmp_path) in rec["repo_root"]

    # The comment body names the job id and carries the HTML tag C4 reads.
    _, _, _, comment_body = calls[1]
    assert rec["id"] in comment_body
    assert "rae-local-agent:job_id=" in comment_body


def test_handle_issue_accept_label_failure_does_not_enqueue(
    tmp_path, monkeypatch, _jobs_dir, caplog
):
    """Label is the idempotency gate — if it fails, nothing else happens."""
    body = _good_body(tmp_path)
    issue = gh_api.Issue(number=7, title="t", url="u", body=body)
    monkeypatch.setattr(
        gh_api,
        "add_label",
        lambda *a: (_ for _ in ()).throw(gh_api.GhCliError("boom")),
    )
    comments: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda *a: comments.append(a))
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, config.RepoConfig(path=tmp_path))

    # Nothing written to pending.
    assert list((_jobs_dir / "pending").iterdir()) == []
    # No comment either (the "Enqueued as X" comment would be a lie).
    assert comments == []
    assert any("adopt label on owner/repo#7 failed" in r.message for r in caplog.records)


def test_handle_issue_accept_comment_failure_logs_but_leaves_job_queued(
    tmp_path, monkeypatch, _jobs_dir, caplog
):
    """Label + job already on disk; comment failing is a recoverable loss, not a crash."""
    body = _good_body(tmp_path)
    issue = gh_api.Issue(number=7, title="t", url="u", body=body)
    monkeypatch.setattr(gh_api, "add_label", lambda *a: None)
    monkeypatch.setattr(
        gh_api,
        "comment_on_issue",
        lambda *a: (_ for _ in ()).throw(gh_api.GhCliError("rate limited")),
    )
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, config.RepoConfig(path=tmp_path))

    # Job is on disk — worker will still produce a PR.
    assert len(list((_jobs_dir / "pending").iterdir())) == 1
    # Warning names the job id so the operator can grep for it.
    msgs = [r.message for r in caplog.records]
    assert any("enqueue comment on owner/repo#7" in m and "job" in m for m in msgs)


# ---------- _handle_issue: reject paths --------------------------------


def test_handle_issue_rejects_on_parse_error(tmp_path, monkeypatch, _jobs_dir, caplog):
    issue = gh_api.Issue(number=9, title="t", url="u", body="just prose, no frontmatter")
    posted: list = []
    labeled: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda s, n, b: posted.append((s, n, b)))
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: labeled.append((s, n, label)))
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, config.RepoConfig(path=tmp_path))

    # Nothing enqueued.
    assert list((_jobs_dir / "pending").iterdir()) == []
    # Comment names the parse error; label is `agent-invalid`.
    assert len(posted) == 1
    assert "YAML frontmatter" in posted[0][2]
    assert labeled == [("owner/repo", 9, issue_poller.LABEL_INVALID)]


def test_handle_issue_rejects_on_enqueue_error(tmp_path, monkeypatch, _jobs_dir):
    """Parsed OK but repo_root isn't a directory — surface it to the issue.

    _make_job_record runs BEFORE the label write (see docstring), so the
    reject path only applies `agent-invalid`, not both labels.
    """
    body = _good_body(tmp_path)
    issue = gh_api.Issue(number=11, title="t", url="u", body=body)
    posted: list = []
    labeled: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda s, n, b: posted.append((s, n, b)))
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: labeled.append((s, n, label)))

    # repo_root pointing at a file, not a directory, trips _make_job_record.
    bogus_root = tmp_path / "a-file"
    bogus_root.write_text("i am a file")

    issue_poller._handle_issue("owner/repo", issue, config.RepoConfig(path=bogus_root))

    assert list((_jobs_dir / "pending").iterdir()) == []
    assert len(posted) == 1
    assert "enqueue failed" in posted[0][2]
    assert labeled == [("owner/repo", 11, issue_poller.LABEL_INVALID)]


def test_reject_tolerates_comment_failure(tmp_path, monkeypatch, _jobs_dir, caplog):
    """Comment write failing on reject doesn't crash — try the label anyway."""
    issue = gh_api.Issue(number=12, title="t", url="u", body="no fm")
    monkeypatch.setattr(
        gh_api,
        "comment_on_issue",
        lambda *a: (_ for _ in ()).throw(gh_api.GhCliError("flake")),
    )
    labeled: list = []
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: labeled.append((s, n, label)))
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_issue("owner/repo", issue, config.RepoConfig(path=tmp_path))

    assert labeled == [("owner/repo", 12, issue_poller.LABEL_INVALID)]
    assert any("reject comment on owner/repo#12 failed" in r.message for r in caplog.records)


# ---------- signal + sleep ---------------------------------------------


def test_on_signal_sets_stop_flag():
    assert issue_poller._flag.stopped is False
    issue_poller._on_signal(signal.SIGTERM, None)
    assert issue_poller._flag.stopped is True


def test_interruptible_sleep_wakes_promptly_when_flag_is_already_set(monkeypatch):
    calls = []
    monkeypatch.setattr(issue_poller.time, "sleep", lambda s: calls.append(s))
    issue_poller._flag.stopped = True

    issue_poller._interruptible_sleep(300)

    assert calls == []


def test_interruptible_sleep_sleeps_in_chunks(monkeypatch):
    calls = []
    monkeypatch.setattr(issue_poller.time, "sleep", lambda s: calls.append(s))

    issue_poller._interruptible_sleep(5)

    assert calls == [1.0, 1.0, 1.0, 1.0, 1.0]


def test_interruptible_sleep_breaks_when_flag_flips_mid_sleep(monkeypatch):
    calls = []

    def _sleep(s):
        calls.append(s)
        if len(calls) == 2:
            issue_poller._flag.stopped = True

    monkeypatch.setattr(issue_poller.time, "sleep", _sleep)

    issue_poller._interruptible_sleep(10)

    assert calls == [1.0, 1.0]


# ---------- completion-pass helpers ------------------------------------


def test_extract_job_id_finds_tag_in_a_comment():
    comments = [
        "unrelated chatter",
        "Enqueued as `1792-abc`.\n\n<!-- rae-local-agent:job_id=1792-abc -->",
        "later comment",
    ]
    assert issue_poller._extract_job_id(comments) == "1792-abc"


def test_extract_job_id_returns_none_when_absent():
    assert issue_poller._extract_job_id(["one", "two", ""]) is None


def test_extract_job_id_first_match_wins():
    """Only the adopt pass should ever leave this tag; if there are two,
    something weird is happening — take the first (= earliest posted)."""
    comments = [
        "<!-- rae-local-agent:job_id=first -->",
        "<!-- rae-local-agent:job_id=second -->",
    ]
    assert issue_poller._extract_job_id(comments) == "first"


def test_lookup_job_status_returns_none_for_missing_job(tmp_path, monkeypatch):
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path / "jobs")
    for sub in ("pending", "running", "done", "failed"):
        (tmp_path / "jobs" / sub).mkdir(parents=True)
    assert issue_poller._lookup_job_status("nope") is None


def test_lookup_job_status_only_state_for_pending(tmp_path, monkeypatch):
    import json as _json

    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path / "jobs")
    (tmp_path / "jobs" / "pending").mkdir(parents=True)
    (tmp_path / "jobs" / "pending" / "abc.json").write_text(
        _json.dumps({"id": "abc", "spec": "s", "created_at": "x"})
    )
    assert issue_poller._lookup_job_status("abc") == {"state": "pending"}


def test_lookup_job_status_returns_full_artifacts_for_done(tmp_path, monkeypatch):
    import json as _json

    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path / "jobs")
    done = tmp_path / "jobs" / "done" / "abc"
    done.mkdir(parents=True)
    (done / "job.json").write_text(_json.dumps({"id": "abc", "spec": "s", "created_at": "x"}))
    (done / "status.txt").write_text("ok\n")
    (done / "pr_url.txt").write_text("https://github.com/x/y/pull/1\n")
    (done / "diff.patch").write_text("diff --git a/x b/x\n...\n")

    out = issue_poller._lookup_job_status("abc")

    assert out["state"] == "done"
    assert out["status"] == "ok"
    assert out["pr_url"] == "https://github.com/x/y/pull/1"
    assert "diff --git" in out["diff"]
    assert out["log"] == ""


# ---------- completion-pass comment formatter -------------------------


def test_completion_comment_ok_has_pr_url_and_diff_preview():
    status = {
        "state": "done",
        "status": "ok",
        "pr_url": "https://github.com/x/y/pull/42",
        "diff": "diff --git a/f b/f\nindex 1..2 1\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
        "log": "",
    }
    out = issue_poller._completion_comment("job-1", status)
    assert "https://github.com/x/y/pull/42" in out
    assert "```diff" in out
    assert "<details>" in out and "</details>" in out
    assert "rae-local-agent:completion" in out


def test_completion_comment_ok_without_diff_still_names_pr():
    status = {"state": "done", "status": "ok", "pr_url": "https://x", "diff": "", "log": ""}
    out = issue_poller._completion_comment("job-1", status)
    assert "https://x" in out
    assert "```diff" not in out
    assert "rae-local-agent:completion" in out


def test_completion_comment_tests_failed_includes_log_head():
    log_body = "\n".join(f"line {i}" for i in range(1, 31))  # 30 lines
    status = {
        "state": "failed",
        "status": "tests_failed",
        "pr_url": "",
        "diff": "",
        "log": log_body,
    }
    out = issue_poller._completion_comment("job-1", status)
    assert "Tests failed" in out
    assert "line 1" in out
    assert "line 20" in out
    assert "line 21" not in out  # truncated at 20
    assert "jobs/failed/job-1/log.txt" in out


def test_completion_comment_tests_failed_handles_missing_log():
    status = {"state": "failed", "status": "tests_failed", "pr_url": "", "diff": "", "log": ""}
    out = issue_poller._completion_comment("job-1", status)
    assert "emitted no output" in out


def test_completion_comment_model_declined():
    status = {"state": "done", "status": "model_declined", "pr_url": "", "diff": "", "log": ""}
    out = issue_poller._completion_comment("job-1", status)
    assert "no diff" in out
    assert "rae-local-agent:completion" in out


def test_completion_comment_error_verbatim():
    status = {
        "state": "done",
        "status": "error:RuntimeError: boom",
        "pr_url": "",
        "diff": "",
        "log": "",
    }
    out = issue_poller._completion_comment("job-1", status)
    assert "error:RuntimeError: boom" in out


def test_completion_comment_unknown_status_handled_gracefully():
    """A status string we don't recognize gets reported, not an exception."""
    status = {"state": "done", "status": "wat", "pr_url": "", "diff": "", "log": ""}
    out = issue_poller._completion_comment("job-1", status)
    assert "unknown status" in out
    assert "wat" in out


# ---------- _handle_completion -----------------------------------------


_ADOPT_COMMENT = "Enqueued as `job-1`.\n\n<!-- rae-local-agent:job_id=job-1 -->"


def _done_ok_fixture(tmp_path, monkeypatch):
    """Write a done/ok job to disk and return the issue + wire the gh mocks."""
    import json as _json

    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path / "jobs")
    done = tmp_path / "jobs" / "done" / "job-1"
    done.mkdir(parents=True)
    (done / "job.json").write_text(_json.dumps({"id": "job-1", "spec": "s", "created_at": "x"}))
    (done / "status.txt").write_text("ok\n")
    (done / "pr_url.txt").write_text("https://x/pr/1\n")
    (done / "diff.patch").write_text("diff --git a/f b/f\n")


def test_handle_completion_happy_path_posts_comment_then_labels(tmp_path, monkeypatch, caplog):
    _done_ok_fixture(tmp_path, monkeypatch)
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    calls: list[tuple] = []
    monkeypatch.setattr(gh_api, "get_issue_comments", lambda s, n: [_ADOPT_COMMENT])
    monkeypatch.setattr(
        gh_api,
        "comment_on_issue",
        lambda s, n, body: calls.append(("comment", body)),
    )
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: calls.append(("label", label)))
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller._handle_completion("owner/repo", issue)

    kinds = [c[0] for c in calls]
    assert kinds == ["comment", "label"]
    _, posted = calls[0]
    assert "https://x/pr/1" in posted
    assert "rae-local-agent:completion" in posted
    _, label = calls[1]
    assert label == issue_poller.LABEL_COMPLETE


def test_handle_completion_skips_when_no_job_id_tag_found(tmp_path, monkeypatch, caplog):
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    monkeypatch.setattr(gh_api, "get_issue_comments", lambda s, n: ["unrelated"])
    comments: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda *a: comments.append(a))
    monkeypatch.setattr(gh_api, "add_label", lambda *a: comments.append(a))
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_completion("owner/repo", issue)

    assert comments == []
    msgs = [r.message for r in caplog.records]
    assert any("no job_id tag" in m for m in msgs)


def test_handle_completion_skips_when_job_not_on_disk(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path / "jobs")
    for sub in ("pending", "running", "done", "failed"):
        (tmp_path / "jobs" / sub).mkdir(parents=True)
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    monkeypatch.setattr(gh_api, "get_issue_comments", lambda s, n: [_ADOPT_COMMENT])
    writes: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda *a: writes.append(a))
    monkeypatch.setattr(gh_api, "add_label", lambda *a: writes.append(a))
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_completion("owner/repo", issue)

    assert writes == []
    msgs = [r.message for r in caplog.records]
    assert any("not found on disk" in m for m in msgs)


def test_handle_completion_waits_for_still_running_job(tmp_path, monkeypatch, caplog):
    import json as _json

    monkeypatch.setattr(job, "JOBS_ROOT", tmp_path / "jobs")
    (tmp_path / "jobs" / "running").mkdir(parents=True)
    (tmp_path / "jobs" / "running" / "job-1.json").write_text(
        _json.dumps({"id": "job-1", "spec": "s", "created_at": "x"})
    )
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    monkeypatch.setattr(gh_api, "get_issue_comments", lambda s, n: [_ADOPT_COMMENT])
    writes: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda *a: writes.append(a))
    monkeypatch.setattr(gh_api, "add_label", lambda *a: writes.append(a))
    caplog.set_level(logging.INFO, logger="rae-local-agent.issue_poller")

    issue_poller._handle_completion("owner/repo", issue)

    assert writes == []
    msgs = [r.message for r in caplog.records]
    assert any("still waiting on job job-1 (state=running)" in m for m in msgs)


def test_handle_completion_retries_only_label_when_completion_tag_already_posted(
    tmp_path, monkeypatch
):
    """Earlier tick posted the comment; label write failed. This tick ONLY retries the label."""
    _done_ok_fixture(tmp_path, monkeypatch)
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    monkeypatch.setattr(
        gh_api,
        "get_issue_comments",
        lambda s, n: [
            _ADOPT_COMMENT,
            "Done — draft PR: https://x/pr/1\n\n<!-- rae-local-agent:completion:job_id=job-1 -->",
        ],
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        gh_api,
        "comment_on_issue",
        lambda s, n, b: calls.append(("comment", b)),
    )
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: calls.append(("label", label)))

    issue_poller._handle_completion("owner/repo", issue)

    kinds = [c[0] for c in calls]
    assert kinds == ["label"]  # No duplicate completion comment.


def test_handle_completion_posts_fresh_comment_when_only_old_cycle_tag_present(
    tmp_path, monkeypatch
):
    """A completion tag from a *different* job id (prior retry cycle) must not suppress
    this cycle's completion comment. The per-job scoping is the whole point of D-1."""
    _done_ok_fixture(tmp_path, monkeypatch)
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    monkeypatch.setattr(
        gh_api,
        "get_issue_comments",
        lambda s, n: [
            # Previous cycle's completion comment, pointing at a DIFFERENT job id.
            "Tests failed — ...\n\n<!-- rae-local-agent:completion:job_id=older-cycle -->",
            # This cycle's adopt comment names job-1 (the one on disk).
            _ADOPT_COMMENT,
        ],
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        gh_api,
        "comment_on_issue",
        lambda s, n, b: calls.append(("comment", b)),
    )
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: calls.append(("label", label)))

    issue_poller._handle_completion("owner/repo", issue)

    kinds = [c[0] for c in calls]
    assert kinds == ["comment", "label"]
    _, posted = calls[0]
    assert "<!-- rae-local-agent:completion:job_id=job-1 -->" in posted


def test_handle_completion_ignores_legacy_untagged_completion_comment(tmp_path, monkeypatch):
    """A pre-D-1 completion comment carries the plain `completion` tag with no job id.
    It must NOT be mistaken for this job's own idempotency marker."""
    _done_ok_fixture(tmp_path, monkeypatch)
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    monkeypatch.setattr(
        gh_api,
        "get_issue_comments",
        lambda s, n: [
            # Legacy format from before D-1 — no job_id in the tag.
            "Done — draft PR: https://x/pr/0\n\n<!-- rae-local-agent:completion -->",
            _ADOPT_COMMENT,
        ],
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        gh_api,
        "comment_on_issue",
        lambda s, n, b: calls.append(("comment", b)),
    )
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: calls.append(("label", label)))

    issue_poller._handle_completion("owner/repo", issue)

    kinds = [c[0] for c in calls]
    assert kinds == ["comment", "label"]


def test_handle_completion_label_failure_logs_but_does_not_crash(tmp_path, monkeypatch, caplog):
    _done_ok_fixture(tmp_path, monkeypatch)
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    monkeypatch.setattr(gh_api, "get_issue_comments", lambda s, n: [_ADOPT_COMMENT])
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda *a: None)
    monkeypatch.setattr(
        gh_api,
        "add_label",
        lambda *a: (_ for _ in ()).throw(gh_api.GhCliError("flake")),
    )
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_completion("owner/repo", issue)

    msgs = [r.message for r in caplog.records]
    assert any("complete label on owner/repo#5 failed" in m for m in msgs)


def test_handle_completion_comment_failure_prevents_label(tmp_path, monkeypatch, caplog):
    """If the comment fails, don't label — next tick re-tries both."""
    _done_ok_fixture(tmp_path, monkeypatch)
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    monkeypatch.setattr(gh_api, "get_issue_comments", lambda s, n: [_ADOPT_COMMENT])
    labels: list = []
    monkeypatch.setattr(
        gh_api,
        "comment_on_issue",
        lambda *a: (_ for _ in ()).throw(gh_api.GhCliError("rate limited")),
    )
    monkeypatch.setattr(gh_api, "add_label", lambda s, n, label: labels.append(label))
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_completion("owner/repo", issue)

    assert labels == []
    msgs = [r.message for r in caplog.records]
    assert any("complete comment on owner/repo#5 failed" in m for m in msgs)


def test_handle_completion_comments_fetch_failure_short_circuits(monkeypatch, caplog):
    issue = gh_api.Issue(number=5, title="t", url="u", body="b")
    monkeypatch.setattr(
        gh_api,
        "get_issue_comments",
        lambda *a: (_ for _ in ()).throw(gh_api.GhCliError("down")),
    )
    writes: list = []
    monkeypatch.setattr(gh_api, "comment_on_issue", lambda *a: writes.append(a))
    monkeypatch.setattr(gh_api, "add_label", lambda *a: writes.append(a))
    caplog.set_level(logging.WARNING, logger="rae-local-agent.issue_poller")

    issue_poller._handle_completion("owner/repo", issue)

    assert writes == []
    msgs = [r.message for r in caplog.records]
    assert any("fetching comments for owner/repo#5" in m for m in msgs)
