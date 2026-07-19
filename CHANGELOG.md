# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file is generated with the tool itself: `aicommit changelog --full`.

## [Unreleased]

_No user-facing changes yet._

## [0.1.0] - 2026-07-19

### Added
- **commit:** interactive Conventional Commit generation from the staged diff, with accept / edit / regenerate / pick-alternative loop
- **pr:** pull request title and description with Summary, Changes, Testing and Risks sections
- **changelog:** deterministic, offline Keep a Changelog release notes grouped by Conventional-Commit type, with an optional `--polish` pass
- **hook:** installable `prepare-commit-msg` hook that prefills the message and safely no-ops on merge, squash, amend and `-m` commits
- **llm:** one OpenAI-compatible backend for free NVIDIA NIM or a fully local Ollama model, switchable with `--backend`
- **diff:** binary and lockfile filtering plus progressive truncation to fit the model context window
- **config:** layered `.aicommit.yaml` configuration with defaults, user, project, environment and CLI precedence
