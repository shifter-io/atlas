# Public CI (PUBLIC-CI-1, OPENSOURCE-2 workstream C)

Atlas becomes a public repository (`shifter-io/atlas`). Its heavy CI runs on our own servers
(ciod's one-job `ci-32cpu` droplets). A public repository must never run outside code there, and
outside contributors still need a CI result. This page records the design, what the workflows do,
the settings the owner changes in GitHub, and the risks.

## Who runs where

| Event | `tier1` (ci.yml), `api` (api.yml) | `public` (public.yml, its own PR) |
|---|---|---|
| A maintainer's pull request (a branch of this repository, opened by a person) | our CI server, as before | GitHub's runners, informational |
| A pull request from a fork, or by a bot | GitHub's runner, **fails in seconds** | GitHub's runners |
| Merge-queue test, schedule on `main`, dispatch | our CI server | schedule and dispatch only |

`maestro-policy` and `maestro-queue` already run on GitHub's runners. `maestro-nightly` runs only on
its schedule and on dispatches (its job says so in an `if:`).

## The guard in the workflows

`tier1` and `api` pick their runner from the event payload, which an outside author cannot change:

```
github.event_name != 'pull_request'
  || (github.event.pull_request.head.repo.full_name == github.repository
      && github.event.pull_request.user.type == 'User')
```

A branch of this repository needs write access, so this is "the maintainers' own pull requests".
When it does not hold, the job runs on `ubuntu-latest` and its first step fails with a message: the
check is red, never skipped. A skipped required check counts as passed, and would let an outside pull
request into the merge queue, whose test runs on our servers. `tests/test_ci_triggers.py` holds every
workflow to this: the guard on every self-hosted job of a pull-request workflow, no
`pull_request_target` / `workflow_run` / `issue_comment` trigger, every action pinned to a full commit.

**This guard is not the security boundary.** A fork's pull request runs the workflow files of its own
branch, so a hostile fork can delete the guard. What stops it, in order:

1. GitHub's approval: no workflow runs for an outside contributor's pull request until a maintainer
   approves the run (setting below). Read the pull request's `.github/` changes before approving.
2. The runner group: only `atlas` can use the `atlas-ci` group.
3. The job-started hook on every CI server (Maestro `tools/bin/ci-runner/job-started.sh`): it refuses,
   before checkout, any pull request not opened and pushed by the maintainer's login from a branch of
   this repository. It reads GitHub's event payload, which no workflow can change.
4. One-job ephemeral servers that hold only their single-use runner registration.

## Outside pull requests

They are never merged through the queue directly. A maintainer reads the change (its `public` result
helps) and re-creates it on a branch of this repository as one commit with a
`Co-authored-by: <login> <<id>+<login>@users.noreply.github.com>` line (the contributor's public noreply
address: the identity check passes and GitHub credits them), then closes the outside pull request with a
link. That pull request goes through review, QA, `tier1`, `api` and the queue like every other.

## Settings the owner changes (GitHub UI)

After the transfer, before the repository is made public. Names as GitHub shows them in November 2026.

**Organization `shifter-io` → Settings → Actions → General**
1. *Policies*: choose **Allow shifter-io, and select non-shifter-io, actions and reusable workflows**,
   tick **Allow actions created by GitHub**, leave **Allow actions by Marketplace verified creators**
   unticked, tick **Require actions to be pinned to a full-length commit SHA**. Save.
2. *Approval for running fork pull request workflows from contributors*: choose **Require approval for
   all external contributors**. Save.
3. *Workflow permissions*: **Read repository contents and packages permissions**; untick **Allow GitHub
   Actions to create and approve pull requests**. Save.

**Organization `shifter-io` → Settings → Actions → Runner groups**
4. **New runner group**: name `atlas-ci`; *Repository access* **Selected repositories** → tick `atlas`
   only; tick **Allow public repositories**; *Workflow access* **All workflows**. **Create group**.
5. The **Default** group: in shifter-io it allows public repositories today (it is not unticked), and it
   stays so: three other public shifter-io repositories run on its runners. That is why the guards for
   atlas are the servers' job-started hook (it refuses every job that is not the maintainers' own) and
   GitHub's approval of outside contributors' runs (step 2), not this setting.

**Repository `shifter-io/atlas` → Settings**
6. *Actions → General*: the same three choices as 1 to 3 (the repository cannot be looser than the
   organization; set them here too so the page shows them). While the repository is still private, in
   *Fork pull request workflows in private repositories* leave **Run workflows from fork pull requests**
   unticked.
7. *Code security → Private vulnerability reporting*: **Enable** (SECURITY.md sends reports there).
8. *Rules → Rulesets → the `main` ruleset → Require status checks to pass*: keep `tier1`, `api`,
   `maestro/review`, `maestro/qa`, `maestro-policy`; for `tier1` and `api` open the check's source menu
   and choose **GitHub Actions**, so nothing else can report them. Do not add `public`. Keep **Require
   merge queue** as it is. Save.

**ciod and the hook (workstream A, Maestro; listed so the change is complete)**
9. ciod registers atlas's JIT runners in `atlas-ci` (today every runner goes to the Default group,
   `DEFAULT_RUNNER_GROUP = 1`), and `atlas` moves from the old organisation's to the shifter-io repository
   list. The hook's `ALLOWED_REPOS` gets `shifter-io/atlas`.
10. ciod gives atlas's servers no sccache key: atlas builds no Rust, and today every server of an
    organization gets that organization's read-write cache key.
11. ciod, defence in depth: no server for a queued job whose run is a pull request from a fork or by a
    login the hook would refuse (the run's `event`, `head_repository` and `actor`); report it instead.

## Risks

1. **Order of the switch.** A public repository's jobs get no runner from a group without **Allow
   public repositories**: every `tier1` and `api` would wait, and ciod would keep booting servers for
   jobs that never start. Steps 4 and 9 come before the repository is made public; check on the
   sandbox pair first.
2. **Approving a fork's run is a security decision.** An approved run executes that branch's workflow
   files. With the hook and the runner group in place it still gets only GitHub's runners, but whoever
   approves must read its `.github/` changes.
3. **The hook is the real boundary.** If it were wrong as well, outside code would run on a one-job
   server holding its single-use registration (and, until step 10, an organization's cache key).
4. **Actions logs and artifacts become public**, also for the maintainers' runs (job summaries, timing
   artifacts, runner names). Workflows keep printing no local paths, hosts or addresses.
5. **Adoption is manual work** for each outside pull request; fine while they are rare.
6. **`public` is a subset of `tier1` and `api`** (no performance tests, no coverage gates), so a change
   can pass it and fail `tier1`; the adopted pull request runs the full checks.
7. **Concurrency limits** of the organization's plan can queue `public` behind other jobs on GitHub's
   runners; it never delays the required checks.
