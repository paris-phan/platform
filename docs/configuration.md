# Project configuration and hooks

`platform.json` is strict JSON, schema version 1. Validate it with `python3 scripts/config.py --config PATH` (Python 3.11+, no third-party packages). Unknown fields and duplicate keys are rejected.

## Fields

| Field | Contract |
|---|---|
| `schema_version` | Integer `1` |
| `repository` | Exact caller `owner/repository` |
| `platform.repository` | `paris-phan/platform` |
| `platform.ref` | Approved full lowercase 40-character commit SHA; same pin as consumer workflows |
| `environments` | Exactly `staging` and `production` |
| `environments.NAME.infisical_environment` | Infisical environment slug |
| `environments.NAME.infisical` | Hosted providers require `{host, project_id, identity_id, audience, path}`; optional for secretless custom targets |
| `apps` | Nonempty ordered array of independent applications |
| `apps[].name` | Unique lowercase identifier, 1–63 characters, initial letter |
| `apps[].path` | Existing directory strictly beneath `apps/`, no traversal or symlink escape |
| `apps[].provider` | `render`, `cloudflare` or `custom` |
| `apps[].targets` | Distinct nonempty staging/production provider resource identifiers, not credentials |
| `apps[].commands` | Required `check`, `build`, `deploy`, `verify`; optional `prepare`, `migrate` |

Each environment's `infisical` object contains `host` (`https://us.infisical.com` or `https://eu.infisical.com`), `project_id`, `identity_id`, `audience`, and an absolute secret-folder `path`. Bind the identity's GitHub OIDC trust to the configured audience and `repo:ORG/REPO:environment:staging` or `...:production`; constrain the reusable workflow identity/ref where supported. Grant read access only to the relevant environment/folder. Imports, recursion, and personal overrides are disabled.

The platform exchanges a GitHub JWT for a short-lived Infisical token, masks secret values in Actions logs, and passes them to build/deployment hooks in memory. Hooks must not print or persist secrets, or build staging-specific credentials/endpoints into a bundle intended for production.

## Command protocol

Commands are argv arrays, for example `["python3", "ops.py", "check"]`, run in the application's directory without an implicit shell. Optional `prepare` installs toolchains/dependencies; it must be idempotent and must not rebuild a release during promotion.

| Variable | Meaning |
|---|---|
| `PLATFORM_BUNDLE_DIR` | Absolute **app-specific** bundle directory; build writes here, deployment reads here |
| `PLATFORM_RELEASE` | Source commit SHA |
| `PLATFORM_ENVIRONMENT` | `staging` or `production` |
| `PLATFORM_TARGET` | Environment-specific configured target |
| `PLATFORM_OPERATION` | `deploy` or `rollback` |
| `PLATFORM_TOOLS_DIR` | Absolute directory containing shared `providers.py` |

Hooks do not receive the coordinator's `GH_TOKEN` or `GITHUB_TOKEN`, but they are trusted code on the same runner, not a sandbox. See [security](../SECURITY.md).

Builds produce nonempty, portable file/directory bundles: no symlinks, special files, or executable-mode dependency (artifact transfer does not preserve Unix modes). Containers should store a registry reference with an `@sha256:` digest in `image.txt`; static frontends store compiled assets. Extraction is limited to 2 GiB. Deployment hooks must not mutate bundle contents.

`migrate` runs for all apps before any deploy and never during rollback. It must be idempotent and compatible with the currently running code. `deploy` blocks until rollout completes; `verify` asserts application health/version and exits nonzero on failure. Deployment acceptance alone is not application verification.

## Shared provider helpers

Invoke `providers.py` from the directory in `PLATFORM_TOOLS_DIR`, using explicit argv:

- **Render:** `render --service SERVICE_ID --image-file IMAGE_FILE`. Requires `RENDER_API_KEY`, an image-backed service with automatic deployment disabled, and an image digest whose repository matches the service. Waits for `live` and checks the reported digest; application verification still runs afterward.
- **Cloudflare Pages:** `cloudflare-pages --directory BUNDLE --project PROJECT --source SHA`. Requires `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, and lockfile-pinned `node_modules/.bin/wrangler` installed by the app's `prepare` command. Use separate direct-upload projects per environment, each with production branch `production`, and disable Git integrations. No implicit `npx` download or rebuild.
- Other workloads use project commands under the same artifact, deployment, and verification contract.

References: [Infisical OIDC login](https://infisical.com/docs/api-reference/endpoints/oidc-auth/login), [Infisical secret retrieval](https://infisical.com/docs/api-reference/endpoints/secrets/list), [Render deployment API](https://api-docs.render.com/reference/create-deploy), [Cloudflare Pages direct upload](https://developers.cloudflare.com/pages/get-started/direct-upload/).
