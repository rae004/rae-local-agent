---
name: Local agent task
about: Queue a mechanical code change for the local agent (adopted on the next poll tick)
title: "agent: "
labels: local-agent
---
---
# scope: the file(s) the agent may touch. Prefer a single file — multi-file
# runs are all-or-nothing until Phase E lands. Every key here must start at
# column 0; list items are indented under their key.
scope:
  - path/to/file.py
# PR title the agent will use (conventional-commit style).
title: "chore: short description of the change"
# Other optional keys: base_branch, test_cmd OR gate_cmds (not both),
# skip_tests, body, group_id. Omit them to use the repo defaults.
---

Describe the change here. This prose becomes the spec the local model
follows, so make it a tight, mechanical before/after — quote the exact
code to replace and the exact code to replace it with, and say "Make no
other changes." Open-ended asks ("improve", "clean up") usually come
back as `model_declined`.
