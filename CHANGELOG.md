# Changelog

All notable changes to this project will be documented here.

## 0.1.0b0 - Unreleased

### Added

- Standalone `AgentTask`, `AgentConfig`, and `run_agent()` API.
- Global and app-specific prompt routing from explicit app identifiers.
- Optional capabilities for Baserow, BigCapital, code-server, Metabase,
  OpenProject, and Twenty.
- Repeated-action loop protection and termination classification.
- LLM usage, request ID, schema-recovery, and opt-in message recording.
- Minimal YAML/JSON CLI and examples.
- A self-contained README covering architecture, reliability mechanisms,
  capability routing, configuration, and evaluation boundaries.
- A Simplified Chinese README for maintainer review and public documentation.
- Unit tests, CI, provenance, security guidance, and public-tree audit.

### Changed

- Unified the public project, distribution, CLI, documentation, and runtime
  branding under `Intelligence Indeed Agent`.

### Removed From The Extraction

- Task datasets and task loaders.
- Application container orchestration and worker scheduling.
- Evaluation, scoring, reporting, and verifier modifications.
- Internal experiment results and operational server scripts.
