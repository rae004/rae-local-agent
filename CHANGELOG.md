# Changelog

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
