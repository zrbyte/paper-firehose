# Agent contribution guidelines

- Always make edits in-place with clear inline comments in your pull request description summarizing what changed and why.
- For every code change, include concise inline notes in the PR diff comments that point reviewers to the important lines and rationale.
- Prefer minimal changes that preserve existing behavior unless the user explicitly requested a refactor.
- When introducing new files or changing public interfaces (CLI flags, outputs, or file formats), update documentation accordingly.

## Major changes protocol

If the change affects behavior, inputs/outputs, dependencies, configuration files, or deployment:
- Update `README.md` with a short summary of the change and any new usage instructions.
- Note migration steps if any
- Add a brief changelog entry to the PR description.

## Style

- Match existing code style and keep code readable and explicit.
- Avoid adding environment variables by default; prefer file-based configuration when possible.
