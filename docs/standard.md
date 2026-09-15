# Platform standard

Share release tooling, not infrastructure ownership or credentials. Projects reference the platform revision they use and document local differences; they do not copy the platform implementation.

The platform owns configuration validation, release identification, promotion checks, deployment coordination, and history. Projects own application commands, resources, secrets, access policy, and when to upgrade.

## Required release guarantees

- Independent applications live under `apps/`, with separate staging and production targets.
- A push to `main` builds, deploys, and verifies staging. Production selects that successful staging run, and its source must be in `production` history.
- Production promotes existing artifacts without rebuilding. Provenance is checked against GitHub Actions; the manifest binds repository, source, run/attempt, configuration, and file hashes.
- Deployments and rollbacks use the same lock per environment. GitHub Actions is the deployment path; disable competing provider triggers.
- Failures stop the rollout and remain visible. Rollback requires a previously successful production release, including its staging attempt and bundle hash.
- Migrations are idempotent and expand-only. Applications tolerate mixed versions during rollout and remain compatible with the database throughout the rollback window. Old migrations never run during rollback.
- Workflow dependencies use full commit-SHA pins. PR validation receives neither deployment credentials nor OIDC permission.
- Deployment secrets belong to the project and environment. Hosted providers use Infisical with scoped OIDC identities; secretless custom targets need no identity.

## Repository settings

Protect `main` and `production` against force pushes and deletion. Require the actual CI check after it has run (`check / platform-check` with the supplied consumer example). Keep merge commits enabled for promotion ancestry. Default workflow tokens to read-only, disable workflow PR approvals, and enable secret scanning/push protection where available.

Use GitHub Environment branch policies of type `branch`: only `main` for staging, only `production` for production. For a team, require independent PR/code-owner review, resolved conversations, and a production environment reviewer with self-review and administrative bypass disabled. For a solo project, explicit promotion and manual production dispatch provide the operator gate; do not configure an impossible second-person approval requirement. Keep administrative access narrow in either case.

The workflows consume these settings; they do not create or reconcile them. Review workflow and deployment-hook changes as code with access to production credentials.

## Defaults and project choices

Defaults are Render image-backed APIs/workers, Cloudflare Pages direct-upload frontends and domains, Infisical Cloud, and GitHub Actions on Ubuntu 24.04. Consumer workflows may select a compatible GitHub Actions runner label through the reusable workflows' `runner` input; the project owns that runner's availability and trust. Bundles request 90-day artifact retention; project registries must retain referenced image digests for the promised recovery window.

Languages, frameworks, dependency installation, resource topology, tests, migration logic, health checks, and upgrade timing remain project choices. Applications deploy in configuration order. Other workloads use the same command contract rather than project-specific branches in the shared workflows.

Document deliberate local policy differences in the project's README. Such documentation does not bypass the release integrity and authorization checks above.
