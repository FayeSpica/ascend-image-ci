"""Failure gates for nightly publication (no registry writes)."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import omni_nightly as nightly

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
ENV = {'CANDIDATE_TAG': 'nightly-test', 'OMNI_SHA': SHA,
       'SOURCE_AUTH': '/tmp/source-auth', 'DEST_AUTH': '/tmp/dest-auth',
       'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'c' * 40}


def manifest(architectures=('amd64', 'arm64')):
    return json.dumps({'manifests': [
        {'digest': DIGEST, 'platform': {'os': 'linux', 'architecture': arch}}
        for arch in architectures]}).encode()


class NightlyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.log = patch.object(nightly, 'summary').start()
        self.addCleanup(patch.stopall)

    def test_missing_architecture_rejected(self):
        with patch.object(nightly, 'run', return_value=manifest(('amd64',))):
            with self.assertRaisesRegex(ValueError, 'missing'):
                nightly.inspect('quay.io/test/image:tag', '/tmp/auth')

    def test_revision_checked_for_both_architectures(self):
        config = json.dumps({'config': {'Labels': {'org.opencontainers.image.revision': SHA}}}).encode()
        raw = manifest()
        with patch.object(nightly, 'run', side_effect=[raw, config, config]) as run:
            self.assertEqual(nightly.inspect(nightly.SOURCE + ':tag', '/tmp/auth', SHA),
                             'sha256:' + hashlib.sha256(raw).hexdigest())
            self.assertEqual(run.call_count, 3)
        with patch.object(nightly, 'run', side_effect=[raw, config, b'{"config":{}}']):
            with self.assertRaisesRegex(ValueError, 'revision mismatch'):
                nightly.inspect(nightly.SOURCE + ':tag', '/tmp/auth', SHA)

    def test_pinned_digest_mismatch_rejected(self):
        with patch.object(nightly, 'run', return_value=manifest()):
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                nightly.inspect(nightly.SOURCE + '@' + DIGEST, '/tmp/auth')

    def test_invalid_upstream_sha_stops_before_base_resolution(self):
        for result in (b'', b'not-a-sha refs/heads/main', (SHA + ' refs/heads/wrong').encode()):
            with self.subTest(result=result), patch.object(nightly, 'run', return_value=result), \
                    patch.object(nightly, 'inspect') as inspect:
                with self.assertRaises(ValueError):
                    nightly.prepare()
                inspect.assert_not_called()

    def test_default_bases_and_shared_sha(self):
        with patch.object(nightly, 'run', return_value=(SHA + '\trefs/heads/main\n').encode()), \
                patch.object(nightly, 'inspect', return_value=DIGEST) as inspect, \
                patch.object(nightly, 'output') as output:
            nightly.prepare()
            self.assertEqual([c.args[0] for c in inspect.call_args_list], [
                'quay.io/atlas-ci/vllm-ascend:v0.28.0' + suffix for _, suffix in nightly.VARIANTS])
            outputs = dict(c.args for c in output.call_args_list)
            self.assertEqual(outputs['sha'], SHA)
            self.assertTrue(outputs['tag'].endswith('-123-1'))
            self.assertEqual(len(json.loads(outputs['matrix'])['include']), 4)

    def test_base_failure_emits_no_build_outputs(self):
        with patch.object(nightly, 'run', return_value=(SHA + '\trefs/heads/main\n').encode()), \
                patch.object(nightly, 'inspect', side_effect=ValueError('missing base')), \
                patch.object(nightly, 'output') as output:
            with self.assertRaises(ValueError):
                nightly.prepare()
            output.assert_not_called()

    def test_last_candidate_failure_prevents_all_copies(self):
        with patch.object(nightly, 'inspect', side_effect=[DIGEST] * 3 + [ValueError('bad candidate')]), \
                patch.object(nightly, 'run') as run:
            with self.assertRaises(ValueError):
                nightly.publish()
            run.assert_not_called()

    def test_copy_failure_stops_remaining_publication(self):
        with patch.object(nightly, 'inspect', return_value=DIGEST), \
                patch.object(nightly, 'run', side_effect=subprocess.CalledProcessError(1, 'skopeo')) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                nightly.publish()
            self.assertEqual(run.call_count, 1)
            self.assertTrue(run.call_args.args[-1].endswith(':nightly'))

    def test_destination_mismatch_stops_remaining_publication(self):
        with patch.object(nightly, 'inspect', side_effect=[DIGEST] * 4 + ['sha256:bad']), \
                patch.object(nightly, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'expected'):
                nightly.publish()
            self.assertEqual(run.call_count, 1)

    def test_all_candidates_checked_before_copy_and_only_rolling_published(self):
        events = []
        def inspect(ref, *args, **kwargs):
            events.append(('inspect', ref))
            return DIGEST
        def run(*args):
            events.append(('copy', args[-1]))
            self.assertIn('--all', args)
            self.assertIn('--preserve-digests', args)
            self.assertEqual(args[-2], f'docker://{nightly.SOURCE}@{DIGEST}')
        with patch.object(nightly, 'inspect', side_effect=inspect), patch.object(nightly, 'run', side_effect=run):
            nightly.publish()
        self.assertEqual([kind for kind, _ in events[:4]], ['inspect'] * 4)
        copies = [ref for kind, ref in events if kind == 'copy']
        self.assertEqual(copies, [f'docker://{nightly.DEST}:nightly{suffix}'
                                for _, suffix in nightly.VARIANTS])

    def test_dockerfile_checkout_sha_and_branch(self):
        dockerfile = Path('docker/vllm-omni/Dockerfile.npu').read_text()
        checkout = dockerfile.split('RUN set -euo pipefail; ', 1)[1].split('\nWORKDIR', 1)[0]
        checkout = 'set -euo pipefail; ' + checkout.replace('\\\n', '')
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'source'
            subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                            'commit', '--allow-empty', '-m', 'first'], check=True, capture_output=True)
            first = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                            'commit', '--allow-empty', '-m', 'second'], check=True, capture_output=True)
            head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
            subprocess.run(['git', '-C', str(repo), 'tag', 'release', first], check=True)
            for name, commit, ref, expected in [('pinned', first, 'main', first),
                                                 ('branch', '', 'main', head),
                                                 ('tag', '', 'release', first)]:
                env = dict(os.environ, VLLM_OMNI_COMMIT=commit, VLLM_OMNI_REF=ref,
                           VLLM_OMNI_REPO=repo.as_uri(), APP_DIR=str(Path(tmp) / name))
                subprocess.run(['bash', '-c', checkout], env=env, check=True, capture_output=True)
                actual = subprocess.check_output(['git', '-C', env['APP_DIR'], 'rev-parse', 'HEAD']).decode().strip()
                self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
