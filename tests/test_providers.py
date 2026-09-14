import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import providers



class SecretBoundary(unittest.TestCase):
    def environment(self):
        return {'infisical_environment': 'production', 'infisical': {
            'host': 'https://us.infisical.com', 'project_id': 'project', 'identity_id': 'identity',
            'audience': 'project-production', 'path': '/deploy'}}

    def test_hidden_duplicate_and_reserved_secrets_are_rejected(self):
        for rows in (
            [{'secretKey': 'PATH', 'secretValue': '/attacker'}],
            [{'secretKey': 'TOKEN', 'secretValue': 'hidden', 'secretValueHidden': True}],
            [{'secretKey': 'TOKEN', 'secretValue': 'one'}, {'secretKey': 'TOKEN', 'secretValue': 'two'}],
        ):
            with self.subTest(rows=rows), patch.dict(os.environ, {
                'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://example.invalid/token',
                'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'github-oidc-token'}, clear=True), patch.object(
                    providers, 'request', side_effect=[{'value': 'jwt'}, {'accessToken': 'short-lived'}, {'secrets': rows}]):
                with self.assertRaises(ValueError):
                    providers.secrets(self.environment())

    def test_render_never_deploys_mutable_image_tags(self):
        with patch.object(providers, 'request', side_effect=AssertionError('provider mutation attempted')):
            with self.assertRaisesRegex(ValueError, 'immutable image'):
                providers.render('srv-example', 'registry.example/api:latest')

    def test_render_refuses_competing_auto_deploy(self):
        with patch.dict(os.environ, {'RENDER_API_KEY': 'test-only'}), patch.object(
                providers, 'request', return_value={'autoDeploy': 'yes'}):
            with self.assertRaisesRegex(ValueError, 'automatic deployment'):
                providers.render('srv-example', 'registry.example/api@sha256:' + 'a' * 64)


if __name__ == '__main__':
    unittest.main()
