# Release operations

## Promote a release

Promotion has two steps: the staging source must reach `production` history through a **merge commit** (squash/rebase does not preserve the ancestry required by promotion authorization), and `platform-production.yml` must be dispatched on `production` with the successful staging run ID and a reason.

### By pull request

Copy `platform-promote.yml` and `platform-ship.yml` from the [workflow examples](../examples/workflows). After every staging run, `promote.yml` opens or refreshes the single `main -> production` pull request, records `Staging-Run: <id>` in its body, and marks it ready; a failed staging run converts it to a draft and comments, leaving it pointed at the last verified run. Merging the pull request is the promotion: `ship.yml` reads the run ID from the merged pull request and dispatches `platform-production.yml`, which performs the same authorization and provenance checks as a manual dispatch. A push to `production` that is not a merged promotion pull request fails `ship.yml` and deploys nothing.

Both workflows use only the workflow token (`pull-requests: write` for promote, `actions: write` for ship) and never receive deployment credentials. Because the workflow token opens the pull request, the consumer's `pull_request` workflows do not run on it; every commit it carries already passed the consumer's checks on `main` and a full staging release. Keep the `Staging-Run` line intact; edit the body elsewhere if needed.

### Manually

1. Identify a successful `platform-staging.yml` run. Record its run ID, attempt, and source SHA in the promotion PR.
2. Merge `main` into `production` with a merge commit.
3. Dispatch `platform-production.yml` on `production`, selecting that staging run and a reason. Approve the production Environment if it has a review gate configured.

For example, from the project checkout with GitHub CLI installed:

```sh
gh workflow run platform-production.yml --ref production \
  -f staging-run-id=RUN_ID -f reason='Release reference'
```

The manual path remains available alongside promotion by pull request, for example to promote an older verified run.

In both cases the coordinator checks the successful main-push workflow, source ancestry, artifact provenance and hashes, then deploys the existing bundle using the staged source's configuration and hooks. It does not substitute a newer main build. Moving backward from the last successful production source requires rollback instead.

Deployment records contain the source, environment, staging run/attempt, bundle hash, operation, reason, and workflow URL. Success means application verification passed at that time, not continuous health.

## Failure and concurrency

Rollout failures stop later work and mark the deployment failed. Earlier services or migrations may already have changed: inspect the workflow and provider state before retrying. There is no automatic rollback or cross-service atomic transaction.

A killed runner or timeout may leave a deployment marked `in_progress`. Treat it as unknown, inspect provider state, and reconcile the record rather than assuming success or dispatching competing work blindly.

Production deployment and rollback share a concurrency key; running jobs are not canceled by newer releases. GitHub retains only one pending execution and may replace it, so this is not a FIFO queue. Not every main commit is guaranteed to deploy. Provider-side manual changes are outside this lock.

## Roll back

1. Select a prior successful production deployment and its staging run/attempt. Confirm that its artifact and registry image still exist and that the current database remains compatible with the old application.
2. Dispatch `platform-rollback.yml` on `production` with that run ID and an incident reason:

   ```sh
   gh workflow run platform-rollback.yml --ref production \
     -f staging-run-id=RUN_ID -f reason='Incident reference'
   ```

3. Complete any environment approval, verify recovery, and fix forward afterward.

Rollback requires the exact source, staging attempt, and bundle hash previously deployed successfully. It uses the existing bundle and runs application verification, **not old migrations**. Application rollback does not revert secrets or provider control-plane configuration.

Do not rerun staging for a retained production release: the current attempt changes, and the previously deployed attempt may no longer qualify. Missing/expired artifacts, missing images, broken ancestry, or lost deployment history fail closed. Produce a new verified release rather than rebuilding and calling it the old one.

## Retention and configuration

Actions artifacts request 90-day retention, subject to repository policy, and extraction is limited to 2 GiB. Images stay in the project's registry; bundles contain immutable digest references. Set the recovery window within both retention limits. Longer-term artifact storage requires a different supported backend; Actions artifacts are not an archive.

Nonsecret configuration changes follow the normal build/staging/promotion path. Rotate secrets in Infisical with an audit reference and deploy the identified release using current secrets. Never snapshot credentials in release bundles. Record emergency provider changes and reconcile them with reviewed configuration.

## Upgrade the platform

Review the selected revision's changes, including configuration and rollback compatibility. Update every consumer workflow `uses` pin, their `platform-ref` inputs, and `platform.json` together to the same full published commit SHA. Run configuration validation, application CI, and staging before promotion; exercise recovery when deployment behavior changes.

Rollback uses the currently pinned coordinator with historical application configuration/hooks. If those versions are incompatible, restore the earlier approved coordinator pin through a reviewed workflow change first. Each project chooses when to upgrade; floating tags must not silently change deployment behavior.
