"""Exercise build orchestration without starting QEMU or using the network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MOCK = r'''#!/usr/bin/python3
import hashlib, json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\n')
fail = os.environ.get('FAIL_TOOL')
if fail == name:
    sys.exit(23)
if name == 'curl':
    target = pathlib.Path(args[args.index('-o') + 1])
    if target.name == 'SHA512SUMS':
        digest = hashlib.sha512(b'image').hexdigest()
        if os.environ.get('BAD_CHECKSUM'): digest = '0' * 128
        image = 'debian-' + os.environ['DEBIAN_VERSION'] + '-genericcloud-amd64.qcow2'
        content = digest + '  ' + image + '\n'
        if os.environ.get('MISSING_CHECKSUM'): content = digest + '  unrelated.qcow2\n'
        target.write_text(content)
    else:
        target.write_bytes(b'image')
elif name == 'virt-sparsify':
    pathlib.Path(args[-1]).write_bytes(b'compressed image')
elif name == 'qemu-img':
    if args[0] == 'info':
        print(json.dumps({'filename': args[-1], 'format': 'qcow2'}))
    elif args[0] == '--version': print('qemu-img mock')
elif name == 'virt-customize':
    if args == ['--version']: print('virt-customize mock')
elif name == 'guestfish':
    commands = sys.stdin.read()
    if 'is-file' in commands:
        print('false\ntrue\n0\nfalse' if os.environ.get('BAD_IMAGE') else 'true\ntrue\n0\nfalse')
    else:
        import shlex
        for line in commands.splitlines():
            _, source, dest = shlex.split(line)
            pathlib.Path(dest).write_text('qemu-guest-agent\t1\n' if source.endswith('packages.txt') else 'VERSION_ID="13"\n')
'''


@unittest.skipIf(os.geteuid() == 0, 'The build intentionally rejects root')
class BuildImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cloud image tests ')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        (self.repo / 'scripts').mkdir(parents=True)
        for name in ('build-image.sh', 'prepare-guest.sh', 'finalize-guest.sh'):
            shutil.copy(ROOT / 'scripts' / name, self.repo / 'scripts' / name)
        # Only the copy operation matters in these orchestration tests.
        (self.repo / 'scripts/pve-import-template.sh').write_text('#!/bin/bash\n')
        shutil.copy(ROOT / 'packages.txt', self.repo / 'packages.txt')
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        for name in ('curl', 'qemu-img', 'virt-customize', 'virt-sparsify', 'guestfish'):
            path = self.bin / name
            path.write_text(MOCK)
            path.chmod(0o755)
        self.calls = self.base / 'calls.jsonl'
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                        CALL_LOG=str(self.calls), DEBIAN_VERSION='13', TIMEZONE='Asia/Shanghai',
                        TMPDIR=str(self.base), GITHUB_SHA='a' * 40,
                        OUTPUT_DIR=str(self.repo / 'dist'), LOG_DIR=str(self.repo / 'logs'),
                        LIBGUESTFS_CACHEDIR=str(self.base / 'cache'))

    def run_build(self, **overrides):
        return subprocess.run(['bash', str(self.repo / 'scripts/build-image.sh')],
                              env=dict(self.env, **overrides), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def assert_no_bundle(self):
        output = self.repo / 'dist'
        self.assertFalse(output.exists() and list(output.iterdir()))

    def test_supported_versions_produce_verified_bundle(self):
        for version in ('12', '13'):
            with self.subTest(version=version):
                out = self.base / f'dist-{version}'
                result = self.run_build(DEBIAN_VERSION=version, OUTPUT_DIR=str(out))
                self.assertEqual(result.returncode, 0, result.stdout)
                metadata = json.loads((out / 'build-info.json').read_text())
                self.assertEqual(metadata['debian_version'], version)
                self.assertEqual(metadata['git_revision'], 'a' * 40)
                info = json.loads((out / 'image-info.json').read_text())
                self.assertEqual(info['filename'], f'debian-{version}-genericcloud-amd64-custom.qcow2')
                check = subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=out,
                                       text=True, capture_output=True)
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
                self.assertNotIn('SHA256SUMS', (out / 'SHA256SUMS').read_text())

    def test_unsupported_version_fails_before_download(self):
        result = self.run_build(DEBIAN_VERSION='10')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_invalid_timezone_fails_before_download(self):
        for timezone in ('Not/AZone', '../etc/passwd', '/etc/passwd', 'UTC;echo unsafe'):
            with self.subTest(timezone=timezone):
                self.assertNotEqual(self.run_build(TIMEZONE=timezone).returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_checksum_mismatch_stops_before_customization(self):
        self.assertNotEqual(self.run_build(BAD_CHECKSUM='1').returncode, 0)
        self.assertFalse(any(c[0] == 'virt-customize' for c in self.recorded()))
        self.assert_no_bundle()

    def test_missing_checksum_stops_before_customization(self):
        self.assertNotEqual(self.run_build(MISSING_CHECKSUM='1').returncode, 0)
        self.assertFalse(any(c[0] == 'virt-customize' for c in self.recorded()))
        self.assert_no_bundle()

    def test_failed_customization_has_no_offline_fallback(self):
        self.assertNotEqual(self.run_build(FAIL_TOOL='virt-customize').returncode, 0)
        calls = self.recorded()
        customize = [c for c in calls if c[0] == 'virt-customize']
        self.assertEqual(len(customize), 1)
        self.assertIn('--network', customize[0])
        self.assertIn('--no-logfile', customize[0])
        self.assertFalse(any(c[0] == 'virt-sparsify' for c in calls))
        self.assert_no_bundle()

    def test_failed_compression_does_not_publish_bundle(self):
        self.assertNotEqual(self.run_build(FAIL_TOOL='virt-sparsify').returncode, 0)
        self.assert_no_bundle()

    def test_failed_image_checks_do_not_publish_bundle(self):
        self.assertNotEqual(self.run_build(BAD_IMAGE='1').returncode, 0)
        self.assert_no_bundle()

    def test_existing_output_is_not_overwritten(self):
        out = self.repo / 'dist'
        out.mkdir()
        (out / 'keep.txt').write_text('keep me')
        self.assertNotEqual(self.run_build().returncode, 0)
        self.assertEqual((out / 'keep.txt').read_text(), 'keep me')
        self.assertEqual(self.recorded(), [])

    def test_download_error_does_not_publish_bundle(self):
        self.assertNotEqual(self.run_build(FAIL_TOOL='curl').returncode, 0)
        self.assert_no_bundle()


if __name__ == '__main__':
    unittest.main()
