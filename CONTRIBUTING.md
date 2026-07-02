# Contributing to Metron

Thank you for your interest. Before any contribution can be accepted, please
read the two policies below — they are required and exist to keep the
project's licensing options intact.

## 1. Developer Certificate of Origin (DCO)

All commits must be signed off (`git commit -s`), certifying the
[Developer Certificate of Origin 1.1](https://developercertificate.org/).
Pull requests containing commits without a `Signed-off-by:` line will not be
merged.

## 2. Inbound license

By submitting a contribution, you agree that your contribution is licensed to
the project under the **MIT License**, regardless of the project's outbound
license (AGPL-3.0-only; see LICENSE). This permits the project to distribute
your contribution under its current license and under commercial licenses. If
you cannot contribute under these terms, please open an issue instead of a
pull request.

## Scope

This repo is the open Metron core — engine math, the API service, the
multi-tenant schema, broker ingestion. Hosted-service, billing, and
proprietary-overlay code is out of scope here and PRs adding it will be
redirected.

Never commit secrets or real config values — use the `.example` pattern.

## Development

Run the test suite (`pytest`) and the linter (`ruff check .`) before opening a
PR; CI gates on both and must be green. Substantial changes should start as an
issue before any code is written.
