GitHub Actions setup
====================

The repository ships with workflows that run the full paper-firehose pipeline on
a schedule and publish results to GitHub Pages. Follow this checklist when you
fork the project and want to keep your fork in sync with your own topics.

Prepare your configuration
--------------------------

1. Fork the repository and clone it locally.
2. Copy ``github_actions_config/topics/topic-template.yaml`` to create new topic
   files or adjust the existing ones in ``github_actions_config/topics/``.
3. Review ``github_actions_config/config.yaml`` to ensure the feeds referenced by
   your topics exist and are enabled.
4. Commit the YAML changes – the workflows copy the files as-is into the runner’s
   data directory.

Bootstrap the same config locally
---------------------------------

The helper script mirrors the workflow behaviour so you can test locally:

.. code-block:: bash

   python scripts/bootstrap_config.py
   paper-firehose --config ~/.paper_firehose/config/config.yaml status

You may pass ``--data-dir`` to the script if you want to use a different target
directory, or ``--overwrite`` to replace an existing config.

Wire up secrets
---------------

Add repository secrets under *Settings → Secrets and variables → Actions* as
needed:

* ``OPENAI_API_KEY`` – enables abstract and paper-qa summaries.
* ``MAILING_LISTS_YAML`` – serialized contents of ``mailing_lists.yaml`` (optional).
* ``SMTP_PASSWORD`` – password referenced by ``config.email.smtp`` (optional).

If a secret is missing the workflow logs a warning and skips the dependent step.

Set the schedule
----------------

The ``.github/workflows/pages.yml`` workflow runs daily at 04:00 UTC. Adjust the
``schedule.cron`` expression to match your timezone or desired frequency. You
can also trigger the workflow manually from the *Actions* tab via
``workflow_dispatch``.
