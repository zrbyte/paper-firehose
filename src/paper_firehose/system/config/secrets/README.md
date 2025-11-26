# Secrets directory

This folder is copied to `~/.paper_firehose/secrets` on first run.

Place sensitive configuration files here, such as:

- `email_password.env`: contains the SMTP password used by the email sender
- `mailing_lists.yaml`: optional per-recipient routing rules for the email command

Files placed here are not tracked by git when copied to your home directory. Be
sure to tighten file permissions manually if needed.
