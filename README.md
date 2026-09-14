# Platform

Shared tooling and conventions for building, deploying, and operating production projects.

holds all release logic (basically just github actions workflows)

See scripts/ for application specific commands and workflows. i curr use
- GCP (cloud run, container registry, )
- infiscal for secrets management
- render for simple container deployments
- cloudflare workers/pages, R2

Pushes to `main` build and deploy staging. Production promotes the same verified artifact without rebuilding it. Rollback uses a previously successful production release.

## Using it

Add a `platform.json` to your project and copy the [workflow examples](examples/workflows) into `.github/workflows/`, dropping the `.example` suffix. Replace the platform pins with a published commit SHA.

- [Configuration and application commands](docs/configuration.md)
- [Deploying, rolling back, and upgrading](docs/operations.md)
- [Shared conventions](docs/standard.md)
