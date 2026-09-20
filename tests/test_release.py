import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release
from config import load_config


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        shutil.copytree(ROOT / 'tests/fixtures/project', self.project)
        self.config = json.loads((self.project / 'platform.json').read_text())
        self.pin = self.config['platform']['ref']
        for argv in (['git', 'init', '-q'], ['git', 'add', '.'],
                     ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture']):
            subprocess.run(argv, cwd=self.project, check=True, capture_output=True)
        self.sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.project, text=True).strip()
        self.bundle = self.base / 'bundle'
        self.records = []
        self.states = {}
        self.run = {'repository': {'full_name': 'example/project'}, 'head_repository': {'full_name': 'example/project'},
                    'event': 'push', 'head_branch': 'main', 'conclusion': 'success', 'status': 'completed',
                    'path': '.github/workflows/platform-staging.yml', 'head_sha': self.sha, 'run_attempt': 1}
        self.env = {'GITHUB_REPOSITORY': 'example/project', 'GITHUB_REF': 'refs/heads/main',
                    'GITHUB_EVENT_NAME': 'push', 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_OUTPUT': str(self.base / 'outputs'), 'PLATFORM_REF': self.pin,
                    'PLATFORM_EXAMPLE_STATE': str(self.base / 'state')}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env, clear=True).start()
        patch('release.api', side_effect=self.api).start()

    def api(self, path, data=None, method=None):
        if path.endswith('/deployments') and data:
            record = dict(data, id=len(self.records) + 1, sha=data['ref'])
            self.records.append(record)
            self.states[record['id']] = []
            return record
        if '/statuses' in path:
            identifier = int(path.split('/deployments/')[1].split('/')[0])
            if data:
                self.states[identifier].insert(0, data)
            return self.states[identifier]
        if '/deployments?' in path:
            return [r for r in reversed(self.records) if r['environment'] == 'production']
        if '/compare/' in path:
            return {'status': 'identical'}
        if '/artifacts?' in path:
            return {'artifacts': [{'id': 7, 'name': 'release-42-1', 'expired': False}]}
        if path.endswith('/actions/runs/42'):
            return copy.deepcopy(self.run)
        raise AssertionError('unexpected API request ' + path)

    def invoke(self, operation, bundle=None, *extra):
        args = ['release.py', operation, '--root', str(self.project), '--bundle', str(bundle or self.bundle), '--run-id', '42', *extra]
        with patch.object(sys, 'argv', args):
            release.main()

    def archive(self):
        archive = self.base / 'release.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for file in self.bundle.rglob('*'):
                if file.is_file():
                    z.write(file, file.relative_to(self.bundle))
        return archive

    def resolve(self, destination, rollback=False):
        archive = self.archive()
        with patch('release.download', side_effect=lambda repo, identifier, path: shutil.copyfile(archive, path)):
            release.resolve('example/project', '42', destination, rollback, 'incident-123')

    def test_complete_lifecycle_promotes_bytes_without_rebuild(self):
        self.invoke('check')
        self.invoke('stage')
        staged = (self.base / 'state/staging/index.html').read_bytes()
        os.environ['GITHUB_REF'] = 'refs/heads/production'
        promoted = self.base / 'promoted'
        self.resolve(promoted)
        # Make rebuilding observably wrong without changing the committed release configuration.
        (self.project / 'apps/site/index.html').write_text('must not rebuild')
        self.invoke('deploy', promoted)
        self.assertEqual((self.base / 'state/production/index.html').read_bytes(), staged)
        self.resolve(self.base / 'rollback', rollback=True)
        self.invoke('deploy', self.base / 'rollback', '--rollback', '--reason', 'incident-123')
        self.assertEqual([r['payload']['operation'] for r in self.records], ['deploy', 'deploy', 'rollback'])
        self.assertTrue(all(self.states[r['id']][0]['state'] == 'success' for r in self.records))

    def test_tampered_artifact_never_reaches_production(self):
        self.invoke('stage')
        (self.bundle / 'site/index.html').write_text('tampered')
        os.environ['GITHUB_REF'] = 'refs/heads/production'
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.resolve(self.base / 'promoted')
        self.assertFalse((self.base / 'state/production').exists())

    def test_rejects_untrusted_or_unsuccessful_staging_provenance(self):
        os.environ['GITHUB_REF'] = 'refs/heads/production'
        for field, value in [('event', 'pull_request'), ('head_branch', 'feature'), ('conclusion', 'failure'),
                             ('path', '.github/workflows/untrusted.yml'),
                             ('head_repository', {'full_name': 'attacker/fork'})]:
            with self.subTest(field=field):
                original = self.run[field]
                self.run[field] = value
                with self.assertRaises(ValueError):
                    release.resolve('example/project', '42', self.base / 'promoted', False, 'release')
                self.run[field] = original

    def test_rollback_requires_prior_successful_production(self):
        os.environ['GITHUB_REF'] = 'refs/heads/production'
        with self.assertRaisesRegex(ValueError, 'no successful production'):
            release.resolve('example/project', '42', self.base / 'rollback', True, 'incident')

    def test_rerun_cannot_impersonate_previous_production_artifact(self):
        self.invoke('stage')
        os.environ['GITHUB_REF'] = 'refs/heads/production'
        self.invoke('deploy')
        self.run['run_attempt'] = 2
        with self.assertRaisesRegex(ValueError, 'no successful production'):
            release.resolve('example/project', '42', self.base / 'rollback', True, 'incident')

    def test_unpromoted_source_is_rejected(self):
        os.environ['GITHUB_REF'] = 'refs/heads/production'
        original = self.api
        def unpromoted(path, data=None, method=None):
            if '/compare/' in path:
                return {'status': 'diverged'}
            return original(path, data, method)
        with patch('release.api', side_effect=unpromoted), self.assertRaisesRegex(ValueError, 'not promoted'):
            release.resolve('example/project', '42', self.base / 'promoted', False, 'release')

    def test_foreign_environment_deployment_does_not_block_promotion(self):
        # A job that merely declares `environment: production` (a store build dispatched from the
        # production branch) makes GitHub record a deployment with no payload and the branch head.
        # It is not a release: ordinary promotion must order itself against the coordinator's own
        # last release, not against that merge commit — which no main source is ever "ahead" of.
        self.invoke('stage')
        os.environ['GITHUB_REF'] = 'refs/heads/production'
        self.invoke('deploy')
        foreign = {'id': 99, 'sha': 'f' * 40, 'ref': 'production', 'environment': 'production', 'payload': {}}
        self.records.append(foreign)
        self.states[99] = [{'state': 'success'}]
        original = self.api
        def compare(path, data=None, method=None):
            if '/compare/' in path and path.split('/compare/')[1].startswith('f' * 40):
                return {'status': 'diverged'}
            return original(path, data, method)
        with patch('release.api', side_effect=compare):
            self.resolve(self.base / 'promoted')
        # The same ordering guard still fires when the coordinator's OWN last release is ahead.
        def backwards(path, data=None, method=None):
            if path.endswith(f'/compare/{self.sha}...{self.sha}'):
                return {'status': 'behind'}
            return original(path, data, method)
        with patch('release.api', side_effect=backwards), self.assertRaisesRegex(ValueError, 'backwards'):
            self.resolve(self.base / 'promoted-2')

    def test_failure_records_failed_deployment(self):
        self.invoke('stage')
        os.environ['GITHUB_REF'] = 'refs/heads/production'
        original = release.command
        def failing(app, name, *args, **kwargs):
            if name == 'verify':
                raise subprocess.CalledProcessError(1, 'verify')
            return original(app, name, *args, **kwargs)
        with patch('release.command', side_effect=failing), self.assertRaises(subprocess.CalledProcessError):
            self.invoke('deploy')
        self.assertEqual(self.states[self.records[-1]['id']][0]['state'], 'failure')

    def test_rollback_skips_migrations(self):
        self.invoke('stage')
        original = release.command
        def reject_migration(app, name, *args, **kwargs):
            if name == 'migrate':
                raise AssertionError('old migration ran during rollback')
            return original(app, name, *args, **kwargs)
        with patch('release.command', side_effect=reject_migration):
            release.rollout(self.config, self.project, self.bundle, self.sha, 'production', '42', 'rollback', 'incident')

    def test_duplicate_configuration_keys_fail_closed(self):
        path = self.project / 'platform.json'
        path.write_text('{"schema_version":1,"schema_version":2}')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            load_config(path)

    def test_configuration_rejects_escape_and_shared_targets(self):
        for change in ('escape', 'shared', 'unknown', 'floating'):
            value = copy.deepcopy(self.config)
            if change == 'escape':
                value['apps'][0]['path'] = 'apps/../../outside'
            elif change == 'shared':
                value['apps'][0]['targets']['production'] = value['apps'][0]['targets']['staging']
            elif change == 'unknown':
                value['unexpected'] = True
            elif change == 'floating':
                value['platform']['ref'] = 'main'
            (self.project / 'platform.json').write_text(json.dumps(value))
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_config(self.project / 'platform.json')


class Extraction(unittest.TestCase):
    def test_rejects_traversal_symlinks_and_duplicate_entries(self):
        for kind in ('traversal', 'symlink', 'duplicate', 'normalized-duplicate'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with zipfile.ZipFile(root / 'bundle.zip', 'w') as z:
                    if kind == 'traversal':
                        z.writestr('../escape', 'bad')
                    elif kind == 'symlink':
                        entry = zipfile.ZipInfo('link')
                        entry.external_attr = 0o120777 << 16
                        z.writestr(entry, '/etc/passwd')
                    else:
                        z.writestr('file', 'one')
                        with warnings.catch_warnings():
                            warnings.simplefilter('ignore', UserWarning)
                            z.writestr('./file' if kind == 'normalized-duplicate' else 'file', 'two')
                with self.assertRaises(ValueError):
                    release.safe_extract(root / 'bundle.zip', root / 'extract')
                self.assertFalse((root / 'escape').exists())


if __name__ == '__main__':
    unittest.main()
