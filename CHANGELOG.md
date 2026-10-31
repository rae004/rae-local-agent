# Changelog

## [0.16.0](https://github.com/rae004/rae-local-agent/compare/v0.15.0...v0.16.0) (2026-10-31)


### Features

* **worker:** expand directory entries in scope to source files ([#58](https://github.com/rae004/rae-local-agent/issues/58)) ([5d49949](https://github.com/rae004/rae-local-agent/commit/5d49949d9de7183d14c5fab84f1e95b865213fcb))


### Bug Fixes

* **worker:** split Ollama connect/read timeouts; raise per-file budget to 45m ([#60](https://github.com/rae004/rae-local-agent/issues/60)) ([0dcf43c](https://github.com/rae004/rae-local-agent/commit/0dcf43c7eb62944db663572e6c88399a6a70ba62))

## [0.15.0](https://github.com/rae004/rae-local-agent/compare/v0.14.0...v0.15.0) (2026-10-25)


### Features

* **poller:** per-job completion-tag idempotency ([#53](https://github.com/rae004/rae-local-agent/issues/53)) ([d83b269](https://github.com/rae004/rae-local-agent/commit/d83b2696e61eec6e3388c20ce36557fc84a55a95))

## [0.14.0](https://github.com/rae004/rae-local-agent/compare/v0.13.0...v0.14.0) (2026-10-25)


### Features

* C4 completion comment-back ([#44](https://github.com/rae004/rae-local-agent/issues/44)) ([cf78977](https://github.com/rae004/rae-local-agent/commit/cf789776e6a093e6cfd316da7d7495e1ea17405a))
* gate_cmds — per-repo polyglot gates (D-0) ([#52](https://github.com/rae004/rae-local-agent/issues/52)) ([b07b451](https://github.com/rae004/rae-local-agent/commit/b07b45173c796bade86d06c7db78ca6240e0d976))


### Documentation

* Phase C closeout — C4 runbook landed, README + MILESTONES ([#51](https://github.com/rae004/rae-local-agent/issues/51)) ([fdd53b8](https://github.com/rae004/rae-local-agent/commit/fdd53b893ac335eeac0049a33976c41e499d4207))

## [0.13.0](https://github.com/rae004/rae-local-agent/compare/v0.12.0...v0.13.0) (2026-10-19)


### Features

* C3 issue poller — enqueue + GitHub writes ([#38](https://github.com/rae004/rae-local-agent/issues/38)) ([6b3fc97](https://github.com/rae004/rae-local-agent/commit/6b3fc9792ab559143ea8d45bf991607ce6b61794))

## [0.12.0](https://github.com/rae004/rae-local-agent/compare/v0.11.0...v0.12.0) (2026-10-19)


### Features

* C2 issue body parser ([#36](https://github.com/rae004/rae-local-agent/issues/36)) ([d0bf934](https://github.com/rae004/rae-local-agent/commit/d0bf934c5005770acfebf4df5c3a03a26ab57311))

## [0.11.0](https://github.com/rae004/rae-local-agent/compare/v0.10.0...v0.11.0) (2026-10-19)


### Features

* C1 issue poller — polling loop (read-only) ([#34](https://github.com/rae004/rae-local-agent/issues/34)) ([f73b721](https://github.com/rae004/rae-local-agent/commit/f73b721af031744993132bf8800043f4fa7acb7e))

## [0.10.0](https://github.com/rae004/rae-local-agent/compare/v0.9.1...v0.10.0) (2026-10-18)


### Features

* C0 scaffold for the Phase C issue poller ([#32](https://github.com/rae004/rae-local-agent/issues/32)) ([0500c37](https://github.com/rae004/rae-local-agent/commit/0500c3731b2d70f0db5a05c2d36dfedbd0dab66d))


### Documentation

* Phase C design + milestone slice ([#31](https://github.com/rae004/rae-local-agent/issues/31)) ([3bb7605](https://github.com/rae004/rae-local-agent/commit/3bb7605d06bbfae331159e1f705f1c882e4cae9c))

## [0.9.1](https://github.com/rae004/rae-local-agent/compare/v0.9.0...v0.9.1) (2026-10-18)


### Documentation

* cite rae-budget[#242](https://github.com/rae004/rae-local-agent/issues/242) as the B9 grouping validation ([#29](https://github.com/rae004/rae-local-agent/issues/29)) ([1bcdda4](https://github.com/rae004/rae-local-agent/commit/1bcdda451e9464903c4ecc740dbe5db2e985386c))

## [0.9.0](https://github.com/rae004/rae-local-agent/compare/v0.8.2...v0.9.0) (2026-10-18)


### Features

* group_id on enqueue_job to land a batch as commits on one PR (B9) ([#27](https://github.com/rae004/rae-local-agent/issues/27)) ([caac55b](https://github.com/rae004/rae-local-agent/commit/caac55b9afcc1e81dd24d7f46cdf619b04689098))

## [0.8.2](https://github.com/rae004/rae-local-agent/compare/v0.8.1...v0.8.2) (2026-10-18)


### Bug Fixes

* isolate test-subprocess env from the worker's uv context ([#22](https://github.com/rae004/rae-local-agent/issues/22)) ([#25](https://github.com/rae004/rae-local-agent/issues/25)) ([5f9afe6](https://github.com/rae004/rae-local-agent/commit/5f9afe63c43e93539fed56141103b96e3eda3c71))

## [0.8.1](https://github.com/rae004/rae-local-agent/compare/v0.8.0...v0.8.1) (2026-10-18)


### Documentation

* B8 "First real run" — close out Phase B with the three rae-budget PRs ([#23](https://github.com/rae004/rae-local-agent/issues/23)) ([daaed0c](https://github.com/rae004/rae-local-agent/commit/daaed0c71b0fa77318d1e5969a904a3e33c72bd4))

## [0.8.0](https://github.com/rae004/rae-local-agent/compare/v0.7.0...v0.8.0) (2026-10-18)


### Features

* title/body overrides on enqueue_job for shaped PRs ([#20](https://github.com/rae004/rae-local-agent/issues/20)) ([7fb8b84](https://github.com/rae004/rae-local-agent/commit/7fb8b8426683e598687c55e0494d0ee1f31f01f4))

## [0.7.0](https://github.com/rae004/rae-local-agent/compare/v0.6.0...v0.7.0) (2026-10-18)


### Features

* systemd user service for the worker (B7) ([#18](https://github.com/rae004/rae-local-agent/issues/18)) ([d4953ac](https://github.com/rae004/rae-local-agent/commit/d4953acf4cdb61580800e7b9fad633cbbe0e4323))

## [0.6.0](https://github.com/rae004/rae-local-agent/compare/v0.5.0...v0.6.0) (2026-10-18)


### Features

* get_job_status and list_jobs MCP tools (B6) ([#16](https://github.com/rae004/rae-local-agent/issues/16)) ([5446cfb](https://github.com/rae004/rae-local-agent/commit/5446cfb4ffa72d1a9b30ba148896dca9be34ed6e))

## [0.5.0](https://github.com/rae004/rae-local-agent/compare/v0.4.0...v0.5.0) (2026-10-18)


### Features

* open a draft PR after push (B5) ([#14](https://github.com/rae004/rae-local-agent/issues/14)) ([d37826f](https://github.com/rae004/rae-local-agent/commit/d37826fcb1b3fb6071b30a87ecd2dce3dd60765d))

## [0.4.0](https://github.com/rae004/rae-local-agent/compare/v0.3.0...v0.4.0) (2026-10-18)


### Features

* test gate — tests_failed routes to failed/&lt;id&gt;/ (B4) ([#12](https://github.com/rae004/rae-local-agent/issues/12)) ([eba035a](https://github.com/rae004/rae-local-agent/commit/eba035a006c1418e38298718062f94ad487ca5f3))

## [0.3.0](https://github.com/rae004/rae-local-agent/compare/v0.2.0...v0.3.0) (2026-10-18)


### Features

* worker pushes a branch after applying the diff (B3) ([#10](https://github.com/rae004/rae-local-agent/issues/10)) ([7f962c5](https://github.com/rae004/rae-local-agent/commit/7f962c55c3c2b5ee9456e979b63e1830b0d7516d))

## [0.2.0](https://github.com/rae004/rae-local-agent/compare/v0.1.1...v0.2.0) (2026-10-18)


### Features

* worker runs transformations and writes diff.patch (B2) ([#8](https://github.com/rae004/rae-local-agent/issues/8)) ([6b3cf5c](https://github.com/rae004/rae-local-agent/commit/6b3cf5cec76bc8ea5aaf0b92198e0c1770785016))

## [0.1.1](https://github.com/rae004/rae-local-agent/compare/v0.1.0...v0.1.1) (2026-10-18)


### Documentation

* MILESTONES.md — Phase B plan (walking skeleton, B0–B8) ([4f0bb12](https://github.com/rae004/rae-local-agent/commit/4f0bb12b9324e74c6dcdb59ac57fb7ec515d05cc))
