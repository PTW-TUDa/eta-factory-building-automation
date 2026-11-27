Contributing to ETA Factory Building Automation
===============================================

This guide is for developers and maintainers of this repository. For an overview of the project, see `README.rst <README.rst>`_.

Branches and work items
-----------------------

- ``master`` holds released versions only. Every release is a merge commit from ``development`` with an annotated ``vX.Y.Z`` tag.
- ``development`` collects finished work. Changes reach it only through merge requests, never through direct pushes or local merges.
- Start each change from a work item created with the **Bug** or **Feature** template, then create a merge request with a feature branch from it.

Global variable lists (GVLs)
----------------------------

The GVLs contain system-specific configuration. They are versioned internally but never published: the public GitHub snapshot leaves out the ``GVLs`` folder, and the pipeline fails if a GVL is found anywhere else. Keep all GVLs in that folder.

Feature merge requests
----------------------

Choose the **Feature** merge request template. Its ``## Release notes`` section is checked by the ``feature_metadata`` job and later becomes part of the release notes:

.. code-block:: markdown

   ## Release notes

   ### New Features

   - Add BACnet integration of the air heat pump.

   ### Breaking Changes

   - Rename the heat pump setpoint variable.
     Compatibility: HMI and data logging using the old name must update.
     Migration: Replace the old variable name in all references.

Allowed categories are ``Breaking Changes``, ``New Features``, ``Bug Fixes`` and ``Internal Changes``. Use one bullet per change, remove unused categories and the template comments. Every breaking change needs ``Compatibility`` and ``Migration`` lines. Merge without squashing so the merge commit keeps the description.

Pipelines at a glance
---------------------

GitLab CI runs only for these three events:

.. list-table::
   :header-rows: 1

   * - Event
     - Jobs
     - Purpose
   * - Merge request into ``development``
     - ``feature_metadata``
     - Checks the ``## Release notes`` section of the merge request.
   * - Merge request ``development`` → ``master``
     - ``release_notes``
     - Builds the release-notes draft and checks version, notes and that no GVL would become public.
   * - Push of a tag ``vX.Y.Z`` on ``master``
     - ``verify_tag`` → ``publish_release`` → ``github_snapshot`` → ``github_publish`` (manual)
     - Creates the GitLab Release, prepares the public GitHub commit and, after your review, publishes it.

There are no merge requests to GitHub. The GitLab branch ``mirroring_to_github`` is no longer used; ``github_publish`` writes directly to the branch of the same name on GitHub.

Creating a release
------------------

**A. Prepare on development**

1. Bump ``VERSION`` (e.g. ``1.3.0``) and check ``CITATION.cff``:

   - ``version`` matches the release tag (e.g. ``v1.3.0``).
   - ``repository-code`` points to the GitHub repository.
   - Authors who contributed to the release are appended. Former members may be removed on request.
   - ``preferred-citation`` is kept if users should cite both the publication and the software.

**B. Release merge request (GitLab)**

2. Open a merge request from ``development`` to ``master`` with the **Release** template. Disable squashing.
3. Download the ``release-notes-draft.md`` artifact of the ``release_notes`` job, paste it into the merge request description and improve the wording. Keep the start/end markers and every ``!<MR>#<n>`` link.
4. Rerun ``release_notes`` until it passes, then merge.

**C. Tag (local)**

5. Tag the merge commit on ``master`` and push the tag. This starts the tag pipeline:

   .. code-block:: console

      git checkout master && git pull
      git tag -a v1.3.0 -m "Release v1.3.0"
      git push origin v1.3.0

**D. Tag pipeline (GitLab, CI/CD > Pipelines)**

6. ``verify_tag`` and ``publish_release`` run automatically and create the internal GitLab Release.
7. ``github_snapshot`` runs automatically. It builds the public commit (release without GVLs, without internal history) and **pushes nothing**. Review its artifacts:

   - ``public-changes.diff``: all changes compared to the current GitHub state
   - ``public-files.txt``: every file that will be public
   - ``public-commit.txt``: the SHA of the reviewed commit

8. If everything is fine, start ``github_publish`` with its play button. It pushes exactly the reviewed commit (it aborts if the commit changed) and creates a **draft** GitHub release. This is the point of no return: once ``github_publish`` has run, the code is public on GitHub. The draft in the next step only holds back the release, its notes and the Zenodo archive, not the code.

**E. GitHub**

9. Open the draft release on GitHub (visible only to repository maintainers), adjust the notes (they contain internal merge request links) and publish it. Publishing creates the tag and triggers Zenodo.
10. Check that Zenodo was updated.

Pipeline tooling
----------------

The jobs are defined in ``.gitlab-ci.yml`` and implemented in ``.gitlab/release.py`` (Python standard library only). Run its tests locally with:

.. code-block:: console

   python -m unittest discover -s .gitlab/tests -v

Maintainer setup
----------------

One-time GitLab project settings required by the pipeline:

- **Merge commit message template** (Settings > Merge requests): use the content of ``.gitlab/merge_commit_message.txt``. Without it the release notes are not stored in the merge commits.
- **Squash commits**: do not require squashing, as release merges must be plain merge commits.
- **API access**: ``release_notes`` reads the merge request description through the GitLab API with the CI job token. If that is not permitted, add a masked CI/CD variable ``GITLAB_ACCESS_TOKEN`` (``read_api``). If the job token cannot create releases, add ``RELEASE_ACCESS_TOKEN`` (``api``).
- **Labels**: create the labels ``bug`` and ``feature`` used by the work item templates.
- **GitHub token**: add a fine-grained GitHub token with ``Contents: Read and write`` on the GitHub repository as masked and protected CI/CD variable ``GITHUB_TOKEN``. Protect the ``v*`` tags so the variable is available in tag pipelines.
- **Push mirror**: disable the GitLab push mirror to GitHub. It would push internal tags and history.
- **Approval (optional)**: protect the ``github`` environment (Settings > CI/CD > Protected environments) to restrict who may start ``github_publish``.
