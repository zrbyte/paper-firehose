# GitHub Actions configuration

The scheduled GitHub Actions workflows copy this directory into the runner’s
runtime data directory (`$PAPER_FIREHOSE_DATA_DIR/config`). When you fork the
repository you can edit these YAML files in-place and commit your changes – the
workflows will automatically pick them up.

## Directory layout
- `config.yaml` – global feed definitions, model settings, and other defaults
  applied to every topic.
- `topics/` – one YAML file per topic you want to monitor. The workflow executes
  the pipeline for every topic file in this folder.
- `topics/topic-template.yaml` – scaffold you can copy when creating a new
  topic.

## Customising topics
1. Copy `topics/topic-template.yaml` to a new file (for example
   `topics/batteries.yaml`).
2. Update the `name`, `description`, and the list of feed keys under `feeds`.
   Feed keys must exist in `config.yaml`.
3. Adjust the `filter.pattern` regular expression and `ranking.query` keywords
   so they reflect the papers you care about.
4. Pick output filenames in the `output` section; these control the HTML files
   uploaded to GitHub Pages.

Commit the new or updated topic files – no extra workflow wiring is required.

## Testing changes locally

You can point the CLI at the same config the workflow uses before committing:

```bash
paper-firehose --config "$(pwd)/github_actions_config/config.yaml" status
paper-firehose --config "$(pwd)/github_actions_config/config.yaml" filter --topic your_topic
```

This uses your local data directory, so you can iterate without waiting for the scheduled GitHub Actions run.

Alternatively, run `python scripts/bootstrap_config.py` from the repo root to copy
these files into your runtime data directory (`~/.paper_firehose` by default).

## Required GitHub secrets

If you use email digests or LLM summaries, add the following repository secrets:

- `OPENAI_API_KEY` – enables abstract summarisation and paper-qa steps.
- `MAILING_LISTS_YAML` – inline contents of `mailing_lists.yaml` (optional).
- `SMTP_PASSWORD` – SMTP password referenced by the email step (optional).

If these are omitted the steps simply log a warning. You can run the pipeline
without them when you only filter and rank papers.
