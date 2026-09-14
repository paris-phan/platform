"""Trusted release coordinator; project commands are explicitly trusted deployment code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from config import SHA, load_config, require
from providers import secrets

API = 'https://api.github.com'


def api(path, data=None, method=None):
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    require(token, 'GitHub token required')
    request = urllib.request.Request(API + path, data=None if data is None else json.dumps(data).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'}, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
        return json.loads(body) if body else None


def pages(path, key=None):
    page = 1
    while True:
        response = api(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
        rows = response[key] if key else response
        yield from rows
        if len(rows) < 100:
            break
        page += 1


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(bundle):
    result = {}
    for path in sorted(bundle.rglob('*')):
        require(not path.is_symlink(), 'bundle symlinks forbidden')
        if path.is_file() and path != bundle / 'manifest.json':
            result[path.relative_to(bundle).as_posix()] = digest(path)
        else:
            require(path.is_dir() or path == bundle / 'manifest.json', 'unsupported bundle entry')
    return result


def command(app, name, root, bundle, source, environment, operation='deploy', credentials=None):
    if name not in app['commands']:
        return
    env = dict(os.environ, PLATFORM_BUNDLE_DIR=str(bundle / app['name']), PLATFORM_RELEASE=source,
               PLATFORM_ENVIRONMENT=environment, PLATFORM_TARGET=app['targets'][environment],
               PLATFORM_OPERATION=operation, PLATFORM_TOOLS_DIR=str(Path(__file__).resolve().parent))
    env.pop('GH_TOKEN', None)
    env.pop('GITHUB_TOKEN', None)
    env.update(credentials or {})
    subprocess.run(app['commands'][name], cwd=root / app['path'], env=env, check=True)


def deployment(repo, source, environment, run, operation, reason, run_attempt, bundle_hash):
    data = api(f'/repos/{repo}/deployments', {'ref': source, 'environment': environment,
        'auto_merge': False, 'required_contexts': [], 'description': f'{operation}: {reason}'[:140],
        'production_environment': environment == 'production', 'transient_environment': False,
        'payload': {'staging_run_id': int(run), 'staging_run_attempt': run_attempt,
                    'bundle_sha256': bundle_hash, 'operation': operation, 'reason': reason}})
    return data['id']


def status(repo, identifier, state):
    api(f'/repos/{repo}/deployments/{identifier}/statuses', {'state': state, 'auto_inactive': False,
        'log_url': f"https://github.com/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"})


def rollout(config, root, bundle, source, environment, run, operation, reason, credentials=None):
    value = json.loads((bundle / 'manifest.json').read_text()) if (bundle / 'manifest.json').exists() else {}
    files = inventory(bundle)
    attempt = value.get('run_attempt', int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')))
    bundle_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    identifier = deployment(config['repository'], source, environment, run, operation, reason, attempt, bundle_hash)
    try:
        status(config['repository'], identifier, 'in_progress')
        if credentials is None:
            credentials = secrets(config['environments'][environment])
        for app in config['apps']:
            command(app, 'prepare', root, bundle, source, environment, operation, credentials)
        # Rollback never runs old migrations against a newer database.
        if operation != 'rollback':
            for app in config['apps']:
                command(app, 'migrate', root, bundle, source, environment, operation, credentials)
        for app in config['apps']:
            command(app, 'deploy', root, bundle, source, environment, operation, credentials)
        for app in config['apps']:
            command(app, 'verify', root, bundle, source, environment, operation, credentials)
        require(files == inventory(bundle), 'deployment hooks mutated release bundle')
    except BaseException:
        status(config['repository'], identifier, 'failure')
        raise
    status(config['repository'], identifier, 'success')


def safe_extract(archive, destination):
    with zipfile.ZipFile(archive) as z:
        names = set()
        total = 0
        for entry in z.infolist():
            path = Path(entry.filename)
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in entry.filename,
                    'unsafe artifact path')
            require(path.as_posix() not in names, 'duplicate artifact entry')
            names.add(path.as_posix())
            mode = entry.external_attr >> 16
            require(mode & 0o170000 not in (0o120000, 0o060000, 0o020000, 0o010000, 0o140000), 'unsafe artifact entry type')
            total += entry.file_size
            require(total <= 2 * 1024**3, 'artifact exceeds 2 GiB uncompressed limit')
        z.extractall(destination)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(repo, identifier, destination):
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    request = urllib.request.Request(f'{API}/repos/{repo}/actions/artifacts/{identifier}/zip',
                                    headers={'Authorization': 'Bearer ' + token})
    try:
        urllib.request.build_opener(NoRedirect).open(request, timeout=60)
    except urllib.error.HTTPError as error:
        require(error.code == 302, f'artifact download failed: HTTP {error.code}')
        location = error.headers['Location']
    else:
        raise ValueError('expected signed artifact redirect')
    require(urllib.parse.urlparse(location).scheme == 'https', 'insecure artifact redirect')
    # Never forward GitHub credentials to the signed storage URL.
    with urllib.request.urlopen(location, timeout=120) as response, destination.open('wb') as out:
        shutil.copyfileobj(response, out)


def manifest(bundle):
    value = json.loads((bundle / 'manifest.json').read_text())
    require(value.get('schema_version') == 1 and value.get('verified') is True, 'unverified release')
    require(isinstance(value.get('source'), str) and SHA.fullmatch(value['source']), 'invalid release SHA')
    require(value['files'] == inventory(bundle), 'bundle integrity mismatch')
    return value


def resolve(repo, run_id, bundle, rollback, reason):
    require(os.environ.get('GITHUB_REF') == 'refs/heads/production', 'production and rollback must run on production branch')
    require(str(run_id).isdigit() and int(run_id) > 0, 'invalid staging run ID')
    require(reason.strip(), 'deployment reason required')
    run = api(f'/repos/{repo}/actions/runs/{run_id}')
    require(run['repository']['full_name'] == repo and run['head_repository']['full_name'] == repo, 'foreign release')
    require(run['event'] == 'push' and run['head_branch'] == 'main' and run['conclusion'] == 'success'
            and run['status'] == 'completed', 'release must be a successful main push')
    require(run['path'] == '.github/workflows/platform-staging.yml', 'unexpected staging workflow')
    source = run['head_sha']
    require(SHA.fullmatch(source), 'invalid source SHA')
    comparison = api(f'/repos/{repo}/compare/{source}...production')
    require(comparison['status'] in ('ahead', 'identical'), 'release source is not promoted into production')
    if rollback:
        found = False
        for dep in pages(f'/repos/{repo}/deployments?environment=production'):
            payload = dep.get('payload') or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            if (dep['sha'] == source and str(payload.get('staging_run_id')) == str(run_id)
                    and payload.get('staging_run_attempt') == run['run_attempt']):
                statuses = api(f"/repos/{repo}/deployments/{dep['id']}/statuses?per_page=100")
                if statuses and statuses[0]['state'] == 'success':
                    found = True
                    rollback_hash = payload.get('bundle_sha256')
                    break
        require(found, 'rollback release has no successful production deployment')
    else:
        # A backwards rollout must use recovery authorization, not ordinary promotion.
        for dep in pages(f'/repos/{repo}/deployments?environment=production'):
            states = api(f"/repos/{repo}/deployments/{dep['id']}/statuses?per_page=1")
            if states and states[0]['state'] == 'success':
                order = api(f"/repos/{repo}/compare/{dep['sha']}...{source}")
                require(order['status'] in ('ahead', 'identical'), 'backwards deployment requires rollback')
                break
    expected = f"release-{run_id}-{run['run_attempt']}"
    artifacts = [a for a in pages(f'/repos/{repo}/actions/runs/{run_id}/artifacts', 'artifacts') if a['name'] == expected and not a['expired']]
    require(len(artifacts) == 1, 'release artifact missing, ambiguous, or expired')
    require(not bundle.exists(), 'bundle destination already exists')
    bundle.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / 'bundle.zip'
        download(repo, artifacts[0]['id'], archive)
        safe_extract(archive, bundle)
    value = manifest(bundle)
    require(value['source'] == source and value['repository'] == repo and str(value['run_id']) == str(run_id)
            and value['run_attempt'] == run['run_attempt'], 'artifact provenance mismatch')
    if rollback:
        require(rollback_hash == hashlib.sha256(json.dumps(value['files'], sort_keys=True).encode()).hexdigest(),
                'rollback bundle differs from the previously deployed release')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
        out.write(f'source={source}\n')
    print(f'Authorized {source} from staging run {run_id}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('check', 'stage', 'resolve', 'deploy'))
    parser.add_argument('--config', default='platform.json')
    parser.add_argument('--root', default='.')
    parser.add_argument('--bundle', default='release-bundle')
    parser.add_argument('--run-id', default=os.environ.get('GITHUB_RUN_ID', ''))
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--reason', default='release promotion')
    args = parser.parse_args()
    root, bundle = Path(args.root).resolve(), Path(args.bundle).resolve()
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    if args.operation == 'resolve':
        resolve(repo, args.run_id, bundle, args.rollback, args.reason)
        return
    config_path = root / args.config
    require(config_path.resolve().is_relative_to(root), 'configuration must be inside project checkout')
    config = load_config(config_path, root)
    if repo:
        require(config['repository'] == repo, 'configuration repository mismatch')
    if args.operation in ('check', 'stage') and os.environ.get('PLATFORM_REF'):
        require(config['platform']['ref'] == os.environ['PLATFORM_REF'], 'configuration and workflow platform pins differ')
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if args.operation == 'check':
        for app in config['apps']:
            command(app, 'prepare', root, bundle, source, 'staging')
            command(app, 'check', root, bundle, source, 'staging')
    elif args.operation == 'stage':
        require(os.environ.get('GITHUB_REF') == 'refs/heads/main' and os.environ.get('GITHUB_EVENT_NAME') == 'push', 'staging requires a main push')
        require(not bundle.exists(), 'bundle destination already exists')
        bundle.mkdir(parents=True)
        for app in config['apps']:
            (bundle / app['name']).mkdir()
            command(app, 'prepare', root, bundle, source, 'staging')
            command(app, 'check', root, bundle, source, 'staging')
        credentials = secrets(config['environments']['staging'])
        for app in config['apps']:
            command(app, 'build', root, bundle, source, 'staging', credentials=credentials)
            require(any((bundle / app['name']).iterdir()), f"empty build bundle: {app['name']}")
        files = inventory(bundle)
        rollout(config, root, bundle, source, 'staging', args.run_id, 'deploy', 'main push', credentials)
        value = {'schema_version': 1, 'repository': repo, 'source': source, 'run_id': int(args.run_id),
                 'run_attempt': int(os.environ['GITHUB_RUN_ATTEMPT']), 'config_sha256': digest(config_path),
                 'files': files, 'verified': True}
        (bundle / 'manifest.json').write_text(json.dumps(value, indent=2) + '\n')
    else:
        require(os.environ.get('GITHUB_REF') == 'refs/heads/production', 'production branch required')
        value = manifest(bundle)
        require(value['source'] == source and value['config_sha256'] == digest(config_path), 'checkout/config mismatch')
        require(value['repository'] == repo and str(value['run_id']) == str(args.run_id), 'release mismatch')
        rollout(config, root, bundle, source, 'production', args.run_id,
                'rollback' if args.rollback else 'deploy', args.reason)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f'platform: {error}', file=sys.stderr)
        sys.exit(1)
