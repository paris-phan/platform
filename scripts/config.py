"""Version-one project contract. No third-party runtime dependencies."""
import argparse
import json
import re
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def keys(value, allowed, required, label):
    require(isinstance(value, dict), f"{label}: expected object")
    require(not value.keys() - set(allowed), f"{label}: unknown keys {value.keys() - set(allowed)}")
    require(set(required) <= value.keys(), f"{label}: missing {set(required) - value.keys()}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'duplicate configuration key: {key}')
        result[key] = value
    return result


def load_config(path, root=None):
    path = Path(path).resolve()
    root = Path(root).resolve() if root else path.parent
    c = json.loads(path.read_text(), object_pairs_hook=unique_object)
    keys(c, ('schema_version', 'repository', 'platform', 'apps', 'environments'),
         ('schema_version', 'repository', 'platform', 'apps', 'environments'), 'project')
    require(type(c['schema_version']) is int and c['schema_version'] == 1, 'unsupported schema_version')
    require(isinstance(c['repository'], str) and REPO.fullmatch(c['repository']), 'invalid repository')
    keys(c['platform'], ('repository', 'ref'), ('repository', 'ref'), 'platform')
    require(c['platform']['repository'] == 'paris-phan/platform', 'unsupported platform repository')
    require(isinstance(c['platform']['ref'], str) and SHA.fullmatch(c['platform']['ref']), 'platform.ref must be a full lowercase commit SHA')
    keys(c['environments'], ('staging', 'production'), ('staging', 'production'), 'environments')
    for name, value in c['environments'].items():
        keys(value, ('infisical_environment', 'infisical'), ('infisical_environment',), name)
        require(isinstance(value['infisical_environment'], str) and value['infisical_environment'].strip(), 'invalid Infisical environment')
        if 'infisical' in value:
            identity = value['infisical']
            keys(identity, ('host', 'project_id', 'identity_id', 'audience', 'path'),
                 ('host', 'project_id', 'identity_id', 'audience', 'path'), 'infisical')
            require(all(isinstance(x, str) and x.strip() for x in identity.values()), 'invalid Infisical mapping')
            require(identity['host'] in ('https://us.infisical.com', 'https://eu.infisical.com'), 'unsupported Infisical host')
            require(identity['path'].startswith('/'), 'Infisical path must be absolute')
    require(isinstance(c['apps'], list) and c['apps'], 'apps must be a nonempty array')
    names = set()
    for a in c['apps']:
        keys(a, ('name', 'path', 'provider', 'targets', 'commands'), ('name', 'path', 'provider', 'targets', 'commands'), 'app')
        require(isinstance(a['name'], str) and re.fullmatch(r'[a-z][a-z0-9-]{0,62}', a['name']), 'invalid app name')
        require(a['name'] not in names, 'duplicate app name')
        names.add(a['name'])
        require(isinstance(a['path'], str), 'app path must be string')
        relative = Path(a['path'])
        require(not relative.is_absolute() and relative.parts and relative.parts[0] == 'apps' and len(relative.parts) > 1 and '..' not in relative.parts, 'app path must be below apps/')
        actual = (root / relative).resolve()
        require(actual.is_relative_to(root / 'apps') and actual.is_dir(), 'app path missing or escapes apps/')
        require(a['provider'] in ('render', 'cloudflare', 'custom'), 'invalid provider')
        if a['provider'] != 'custom':
            require(all('infisical' in e for e in c['environments'].values()), 'hosted providers require Infisical OIDC mappings')
        keys(a['targets'], ('staging', 'production'), ('staging', 'production'), 'targets')
        require(all(isinstance(t, str) and t.strip() and '\n' not in t for t in a['targets'].values()), 'targets must be nonempty nonsecret identifiers')
        require(a['targets']['staging'] != a['targets']['production'], 'staging and production targets must differ')
        keys(a['commands'], ('prepare', 'check', 'build', 'migrate', 'deploy', 'verify'), ('check', 'build', 'deploy', 'verify'), 'commands')
        for command in a['commands'].values():
            require(isinstance(command, list) and command and all(isinstance(x, str) and x and '\0' not in x for x in command), 'commands must be nonempty argv arrays')
    return c


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Validate a platform project configuration.')
    parser.add_argument('--config', default='platform.json')
    args = parser.parse_args()
    try:
        load_config(args.config)
    except (ValueError, OSError) as error:
        parser.exit(1, f'platform: {error}\n')
    print('Configuration valid')
