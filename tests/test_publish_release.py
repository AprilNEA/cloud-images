"""Test release orchestration without accessing GitHub."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/publish-release.sh'
SHA = '123456789abc' + 'd' * 28
MOCK_GH = r'''#!/usr/bin/python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['GH_CALLS'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if '--notes-file' in args:
    with open(args[args.index('--notes-file') + 1]) as source:
        with open(os.environ['NOTES_CAPTURE'], 'w') as target:
            target.write(source.read())
operation = args[1]
state = os.environ.get('RELEASE_STATE', 'missing')
if operation == 'view':
    if 'url' in args:
        print('https://github.com/test/images/releases/tag/' + args[2])
    elif state == 'missing':
        sys.exit(1)
    else:
        print(json.dumps({'isDraft': state != 'published', 'targetCommitish':
              'f' * 40 if state == 'wrong-target' else os.environ['GITHUB_SHA']}))
if operation == os.environ.get('FAIL_OPERATION'):
    sys.exit(23)
'''


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dist = self.root / 'dist'
        self.dist.mkdir()
        self.info = {
            'git_revision': SHA, 'debian_version': '13',
            'built_at': '2026-10-20T21:00:00+00:00', 'timezone': 'Asia/Shanghai',
            'source_url': 'https://example.invalid/debian.qcow2', 'source_sha512': 'b' * 128,
        }
        for name in ['debian-13-genericcloud-amd64-custom.qcow2', 'pve-import-template.sh',
                     'image-info.json', 'packages.txt', 'os-release', 'source.sha512']:
            (self.dist / name).write_text('fixture\n')
        self.write_metadata()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        gh.write_text(MOCK_GH)
        gh.chmod(0o755)
        self.calls = self.root / 'calls.jsonl'
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                        GITHUB_REPOSITORY='test/images', GITHUB_SHA=SHA,
                        GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='push',
                        NOTES_CAPTURE=str(self.root / 'notes.md'),
                        GH_CALLS=str(self.calls), GITHUB_STEP_SUMMARY=str(self.root / 'summary.md'))

    def write_metadata(self):
        (self.dist / 'build-info.json').write_text(json.dumps(self.info))
        sums = []
        for path in sorted(self.dist.iterdir()):
            if path.name != 'SHA256SUMS':
                sums.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name)
        (self.dist / 'SHA256SUMS').write_text('\n'.join(sums) + '\n')

    def run_release(self, **env):
        return subprocess.run(['bash', str(SCRIPT), str(self.dist)],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=20)

    def recorded(self):
        return [json.loads(s) for s in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_tag_uses_build_date_in_utc_plus_eight_and_commit(self):
        result = self.run_release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.recorded()
        self.assertEqual([c[1] for c in calls], ['view', 'create', 'upload', 'edit', 'view'])
        create = calls[1]
        self.assertEqual(create[2], 'debian-13-2026-10-21-123456789abc')
        self.assertEqual(create[create.index('--target') + 1], SHA)
        self.assertIn('--draft', create)
        self.assertIn('--draft=false', calls[3])
        self.assertIn('--latest=true', calls[3])
        self.assertIn('https://github.com/test/images/releases/tag/', (self.root / 'summary.md').read_text())
        notes = (self.root / 'notes.md').read_text()
        self.assertTrue(notes.isascii())
        self.assertIn('Source commit:', notes)

    def test_debian_12_does_not_replace_latest_debian_13(self):
        self.info['debian_version'] = '12'
        (self.dist / 'debian-13-genericcloud-amd64-custom.qcow2').rename(
            self.dist / 'debian-12-genericcloud-amd64-custom.qcow2')
        self.write_metadata()
        self.assertEqual(self.run_release().returncode, 0)
        self.assertIn('--latest=false', self.recorded()[3])

    def test_pr_does_not_publish(self):
        self.assertNotEqual(self.run_release(GITHUB_EVENT_NAME='pull_request').returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_non_main_dispatch_does_not_publish(self):
        self.assertNotEqual(self.run_release(GITHUB_EVENT_NAME='workflow_dispatch',
                                            GITHUB_REF='refs/heads/test').returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_checksum_failure_does_not_contact_github(self):
        (self.dist / 'packages.txt').write_text('tampered\n')
        self.assertNotEqual(self.run_release().returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_commit_mismatch_does_not_contact_github(self):
        self.info['git_revision'] = 'f' * 40
        self.write_metadata()
        self.assertNotEqual(self.run_release().returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_published_release_is_not_overwritten(self):
        result = self.run_release(RELEASE_STATE='published')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([c[1] for c in self.recorded()], ['view'])

    def test_existing_matching_draft_can_resume(self):
        self.assertEqual(self.run_release(RELEASE_STATE='draft').returncode, 0)
        self.assertEqual([c[1] for c in self.recorded()], ['view', 'upload', 'edit', 'view'])

    def test_existing_draft_for_another_commit_is_rejected(self):
        self.assertNotEqual(self.run_release(RELEASE_STATE='wrong-target').returncode, 0)
        self.assertEqual([c[1] for c in self.recorded()], ['view'])

    def test_failed_upload_does_not_publish_draft(self):
        self.assertNotEqual(self.run_release(FAIL_OPERATION='upload').returncode, 0)
        self.assertEqual([c[1] for c in self.recorded()], ['view', 'create', 'upload'])

    def test_failed_create_does_not_upload(self):
        self.assertNotEqual(self.run_release(FAIL_OPERATION='create').returncode, 0)
        self.assertEqual([c[1] for c in self.recorded()], ['view', 'create'])

    def test_missing_asset_does_not_contact_github(self):
        (self.dist / 'pve-import-template.sh').unlink()
        self.write_metadata()
        self.assertNotEqual(self.run_release().returncode, 0)
        self.assertEqual(self.recorded(), [])


if __name__ == '__main__':
    unittest.main()
