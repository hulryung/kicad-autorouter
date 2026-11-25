"""Publication refuses corruption, mutable versions and wrong destinations."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publish', ROOT / 'scripts/publish-pcm.py')
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


class Guards(unittest.TestCase):
    def test_catalogue_preserves_other_packages_and_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'packages.json'
            other = {'identifier': 'other', 'versions': [{'version': '8', 'download_sha256': 'other'}]}
            old = {'identifier': 'ours', 'versions': [{'version': '1', 'download_sha256': 'old'}]}
            path.write_text(json.dumps({'packages': [other, old], 'custom': 1}))
            meta = {'identifier': 'ours', 'author': {}, 'versions': [{'version': '2'}]}
            package = dict(zip='v2.zip', download_sha256='new', download_size=12, install_size=20)
            result = publish.prepare_catalogue(path, meta, package, 'https://example.invalid')
            self.assertEqual(result['packages'][0], other)
            self.assertEqual(result['custom'], 1)
            self.assertEqual([v['version'] for v in result['packages'][1]['versions']], ['2', '1'])
            path.write_text(json.dumps(result))
            self.assertEqual(publish.prepare_catalogue(path, meta, package, 'https://example.invalid'), result)
            with self.assertRaisesRegex(RuntimeError, 'bump'):
                publish.prepare_catalogue(path, meta, {**package, 'download_sha256': 'changed'}, '')
            for corrupt in ('{', '[]', '{"packages":{}}', '{"packages":[null]}'):
                path.write_text(corrupt)
                with self.assertRaises(ValueError):
                    publish.prepare_catalogue(path, meta, package, '')
                self.assertEqual(path.read_text(), corrupt)

    def test_checkout_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(['git', '-C', tmp, *args], check=True, capture_output=True)
            git('init', '-q')
            git('symbolic-ref', 'HEAD', 'refs/heads/main')
            git('remote', 'add', 'origin', 'https://github.com/owner/repo.git')
            publish.validate_checkout(tmp, 'owner/repo')
            git('remote', 'set-url', '--push', 'origin', 'https://github.com/wrong/repo.git')
            with self.assertRaisesRegex(RuntimeError, 'origin'):
                publish.validate_checkout(tmp, 'owner/repo')
            git('remote', 'set-url', '--push', 'origin', 'https://github.com/owner/repo.git')
            git('symbolic-ref', 'HEAD', 'refs/heads/topic')
            with self.assertRaisesRegex(RuntimeError, 'main'):
                publish.validate_checkout(tmp, 'owner/repo')


if __name__ == '__main__':
    unittest.main()
