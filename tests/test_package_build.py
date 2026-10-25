"""Release archives are complete, reproducible, and version-consistent."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_package', ROOT / 'scripts/build-package.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class PackageBuildTests(unittest.TestCase):
    def test_reproducible_complete_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            first = build.build_package(Path(directory) / 'one')
            second = build.build_package(Path(directory) / 'two')
            data = Path(first['zip']).read_bytes()
            self.assertEqual(data, Path(second['zip']).read_bytes())
            self.assertEqual(first['download_sha256'], hashlib.sha256(data).hexdigest())
            with zipfile.ZipFile(first['zip']) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), len(set(names)))
                self.assertEqual(archive.read('plugins/autorouter/LICENSE'), (ROOT / 'LICENSE').read_bytes())
                self.assertIn('plugins/autorouter/INSTALL.txt', names)
                self.assertIn('plugins/autorouter/engine/native/islands.py', names)
                self.assertFalse(any('__pycache__' in n or n.endswith(('.pyc', '.devreload', '.kicad_pcb')) for n in names))
                self.assertEqual(first['install_size'], sum(i.file_size for i in archive.infolist()))
                self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in archive.infolist()))
                self.assertEqual(json.loads(archive.read('metadata.json'))['versions'][0]['version'], build.version())
                self.assertIsNone(archive.testzip())

    def test_rejects_version_mismatch(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, 'version', return_value='999.0.0'):
            with self.assertRaisesRegex(AssertionError, 'disagree'):
                build.build_package(directory)
            self.assertFalse(list(Path(directory).glob('*.zip')))


if __name__ == '__main__':
    unittest.main()
