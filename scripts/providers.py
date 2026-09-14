"""Bounded Infisical OIDC and immutable Render deployment integrations."""
import argparse
import json
import os
import re
import time
import urllib.parse
import subprocess
import urllib.request

from config import require


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, token=None, data=None):
    require(urllib.parse.urlparse(url).scheme == 'https', 'HTTPS required')
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, headers=headers, data=None if data is None else json.dumps(data).encode())
    with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as response:
        return json.load(response)


def mask(value):
    if os.environ.get('GITHUB_ACTIONS') == 'true' and value:
        print('::add-mask::' + value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A'), flush=True)


def secrets(environment):
    identity = environment.get('infisical')
    if identity is None:
        return {}
    endpoint = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    token = os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']
    endpoint += ('&' if '?' in endpoint else '?') + urllib.parse.urlencode({'audience': identity['audience']})
    jwt = request(endpoint, token)['value']
    mask(jwt)
    access = request(identity['host'] + '/api/v1/auth/oidc-auth/login', data={
        'identityId': identity['identity_id'], 'jwt': jwt})['accessToken']
    mask(access)
    query = urllib.parse.urlencode({'projectId': identity['project_id'],
        'environment': environment['infisical_environment'], 'secretPath': identity['path'],
        'viewSecretValue': 'true', 'expandSecretReferences': 'true', 'includeImports': 'false',
        'includePersonalOverrides': 'false', 'recursive': 'false'})
    rows = request(identity['host'] + '/api/v4/secrets?' + query, access)['secrets']
    result = {}
    for row in rows:
        key, value = row['secretKey'], row['secretValue']
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key), 'invalid secret environment name')
        require(not key.startswith(('GITHUB_', 'ACTIONS_', 'PLATFORM_', 'RUNNER_', 'PYTHON', 'LD_', 'DYLD_'))
                and key not in ('PATH', 'HOME', 'BASH_ENV', 'ENV', 'GH_TOKEN'), 'reserved secret environment name')
        require(key not in result and not row.get('secretValueHidden'), 'duplicate or hidden secret')
        require(isinstance(value, str) and '\0' not in value, 'invalid secret value')
        mask(value)
        result[key] = value
    return result


def render(service, image, timeout=900):
    require(re.fullmatch(r'srv-[a-zA-Z0-9]+', service), 'invalid Render service ID')
    require(re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', image), 'Render requires an immutable image digest')
    token = os.environ['RENDER_API_KEY']
    base = 'https://api.render.com/v1/services/' + service
    service_data = request(base, token)
    require(service_data.get('autoDeploy') == 'no', 'disable Render automatic deployment before using platform')
    deploy = request(base + '/deploys', token, {'imageUrl': image})
    identifier = deploy['id']
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = request(base + '/deploys/' + urllib.parse.quote(identifier, safe=''), token)
        state = value['status']
        if state == 'live':
            require(value.get('image', {}).get('sha') in (image.split('@')[1], image.split(':')[-1]), 'Render deployed a different image digest')
            print(f'Render deployment {identifier} live')
            return
        require(state not in ('build_failed', 'update_failed', 'pre_deploy_failed', 'canceled', 'deactivated'), 'Render deployment failed: ' + state)
        time.sleep(5)
    raise TimeoutError('Render deployment timed out; inspect provider before retrying')


def cloudflare_pages(directory, project, source):
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]*', project), 'invalid Cloudflare Pages project')
    require(re.fullmatch(r'[0-9a-f]{40}', source), 'invalid release SHA')
    require(os.path.isdir(directory), 'Pages bundle missing')
    require(os.environ.get('CLOUDFLARE_API_TOKEN') and os.environ.get('CLOUDFLARE_ACCOUNT_ID'),
            'Cloudflare project-scoped credentials required')
    # Project prepare installs its lockfile-pinned Wrangler; never download an implicit latest version.
    executable = os.path.abspath('node_modules/.bin/wrangler')
    require(os.path.isfile(executable), 'install lockfile-pinned Wrangler in the application prepare command')
    subprocess.run([executable, 'pages', 'deploy', directory, '--project-name', project,
                    '--branch', 'production', '--commit-hash', source], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='provider', required=True)
    render_parser = sub.add_parser('render')
    render_parser.add_argument('--service', default=os.environ.get('PLATFORM_TARGET'))
    render_parser.add_argument('--image-file', required=True)
    pages_parser = sub.add_parser('cloudflare-pages')
    pages_parser.add_argument('--directory', default=os.environ.get('PLATFORM_BUNDLE_DIR'))
    pages_parser.add_argument('--project', default=os.environ.get('PLATFORM_TARGET'))
    pages_parser.add_argument('--source', default=os.environ.get('PLATFORM_RELEASE'))
    args = parser.parse_args()
    if args.provider == 'render':
        with open(args.image_file) as stream:
            render(args.service, stream.read().strip())
    else:
        cloudflare_pages(args.directory, args.project, args.source)
