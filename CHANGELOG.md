# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.6] - 2026-07-19

### Added
- Added a policy rollout action in the admin UI. It refreshes idle terminals for one policy and reports exactly how many were refreshed and how many active terminals were skipped.

### Changed
- Reworked the admin UI into a cleaner, denser light workspace with fewer borders, tighter rows, clearer policy actions, and a policy editor that keeps Save visible while scrolling.
- Made busy terminal traffic lighter to serve by avoiding repeated health checks, repeated Open WebUI login checks, and repeated Kubernetes status writes during normal proxy use.
- Split proxy connection pools by terminal instance so heavy traffic to one terminal does not slow down unrelated terminal sessions.
- Turned off access logs and WebSocket compression by default to reduce noisy runtime overhead.
- Documented the new runtime settings for status caching, token caching, WebSocket compression, and access logs.

### Fixed
- Fixed packaged installs and Docker images so the built admin UI is included in the Python package. Visiting the root page now loads the UI instead of falling through to the proxy and returning `401 Missing Authorization header`.
- Fixed policy save and delete flows so cached policy and tool details are cleared immediately after a change.
- Fixed HTTP and WebSocket retries so, when a terminal is starting or has been replaced, the proxy re-checks the current terminal before retrying instead of retrying an old address.
- Fixed the active WebSocket connection count so failed connection attempts no longer leave the admin status count permanently too high.
- Fixed retry failure handling so HTTP and WebSocket requests return a clean terminal-unreachable error when a terminal cannot be resolved during retry.

## [0.0.5] - 2026-07-09

### Added
- Added Kubernetes node selector and toleration overrides for terminal and reset pods.

## [0.0.4] - 2026-06-29

### Added
- Added a minimal admin UI for viewing terminal status, active sessions, and policies.
- Added policy lifecycle support, including scheduled resets and lifecycle state tracking.
- Added OpenShift-focused security context controls and deployment documentation.
- Added frontend build packaging to the server Docker image.
- Added terminal environment propagation for system prompts and resource metadata.
- Added configurable server and operator log levels.

### Fixed
- Fixed Docker backend storage limit handling with a best-effort fallback when the host driver cannot enforce quotas.
- Fixed stale proxy connection handling by retrying once after keep-alive failures.
- Fixed Kubernetes and operator provisioning paths to pass effective policy environment values consistently.

## [0.0.1] - 2026-04-02

### Added
- Multi-tenant terminal orchestrator with Docker and Kubernetes backends.
- Kubernetes operator for terminal custom resource management.
- CLI interface for managing terminals.
- Docker build workflows for orchestrator and operator images (multi-arch: amd64/arm64).
