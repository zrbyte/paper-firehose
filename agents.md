# Agent contribution guidelines

- Always make edits in-place with clear inline comments in your pull request description summarizing what changed and why.
- For every code change, include concise inline notes in the PR diff comments that point reviewers to the important lines and rationale.
- Keep prompts and configuration separate from code. Use `llm_prompts.json` for LLM instructions and `search_terms.json` for regex topics.
- Prefer minimal changes that preserve existing behavior unless the user explicitly requested a refactor.
- When introducing new files or changing public interfaces (CLI flags, outputs, or file formats), update documentation accordingly.
- Code should be compatible with Python 3.11, with 3.7 compatibility in branch: "ek-server-version".

## Major changes protocol

If the change affects behavior, inputs/outputs, dependencies, configuration files, or deployment:
- Update `README.md` with a short summary of the change and any new usage instructions.
- Note migration steps if any (e.g., new keys in `llm_prompts.json`).
- Add a brief changelog entry to the PR description.

## Testing

- Run `!python rssparser.py --purge-days 1` and `rssparser.py --no-upload` locally after changes.
- For feed/regex changes, verify that `assets/seen_entries.db` updates as expected, or clear with `--clear-db` before testing.

## Style

- Match existing code style and keep code readable and explicit.
- Avoid adding environment variables by default; prefer file-based configuration when possible.

