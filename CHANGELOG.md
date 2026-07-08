# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-08

Initial release.

### Added
- `klein` CLI: connects to a BehaviorTree.CPP v4 robot node over the Groot2
  ZeroMQ publisher protocol, unrolls nested subtrees into a single tree, and
  streams 10 Hz status telemetry to an interactive D3.js browser dashboard.
- Single-port HTTP + WebSocket server: serves the dashboard and pushes telemetry
  from one `--port`, with the vendored D3.js so it works on air-gapped networks.
- `klein-mock`: a fake Groot2 publisher that drives the dashboard with no real
  robot, for demos and testing.

[Unreleased]: https://github.com/johanubbink/klein-bt/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/johanubbink/klein-bt/releases/tag/v0.1.0
