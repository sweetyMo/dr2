import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import ci
import macos
from source import game_input
from fixtures import runtime_input, source


class MacBuildTests(unittest.TestCase):
    def test_native_changes_reuse_generation_but_changed_generators_do_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, _, _, _ = source(Path(temporary))
            original = macos.generation_key(upstream, 'input', 'xcode')
            (upstream / 'dm/runtime.c').write_text('new native code\n')
            self.assertEqual(macos.generation_key(upstream, 'input', 'xcode'), original)
            (upstream / 'dm/tools/helper.py').write_text('changed_generation = True\n')
            self.assertNotEqual(macos.generation_key(upstream, 'input', 'xcode'), original)
            self.assertNotEqual(macos.generation_key(upstream, 'other-input', 'xcode'), original)

    def test_existing_archive_cache_is_verified_without_downloading_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / '.game-downloads/game-input.archive'
            archive.parent.mkdir()
            archive.write_bytes(b'existing ExeFS/RomFS archive')
            digest = ci.sha256(archive)
            with patch.object(ci, 'ROOT', root), patch.object(game_input, 'settings', return_value=('https://storage.invalid/game.zip', digest)), patch.object(game_input, 'download_file') as download:
                macos.download()
                download.assert_not_called()
            self.assertEqual(archive.read_bytes(), b'existing ExeFS/RomFS archive')

    def test_prepared_cache_integrity_and_macos_executable_platform(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / '.macos-prepared'
            cache.mkdir()
            payload = cache / 'program'
            payload.write_bytes(b'prepared')
            runtime_input(cache / 'romfs-inputs.zip')
            (cache / 'generated').mkdir()
            for name in macos.HEADERS:
                (cache / 'generated' / name).write_text('fixture table\n')
            (cache / 'manifest.json').write_text(json.dumps({'format': 'd2r-mobile-macos-prepared',
                'schema_version': 2, 'key': 'cache-key',
                'files': {p.relative_to(cache).as_posix(): ci.sha256(p)
                          for p in cache.rglob('*') if p.is_file()}}))
            with patch.object(ci, 'ROOT', root), patch.dict(os.environ, MACOS_PREPARED_KEY='cache-key'):
                self.assertEqual(macos.load_prepared()['key'], 'cache-key')
                payload.write_bytes(b'damaged')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    macos.load_prepared()
        native = struct.pack('<8I6I', 0xfeedfacf, 0x0100000c, 0, 2, 1, 24, 0, 0,
                             0x32, 24, 1, 15 << 16, 26 << 16, 0)
        self.assertEqual(ci.macho_minimum_os(native, platform=1), (15, 0, 0))
        with self.assertRaises(ValueError):
            ci.macho_minimum_os(native)

    def test_prepared_archive_allows_native_changes_and_rejects_generation_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, commit, _, _ = source(root)
            package = root / 'package'
            package.mkdir()
            (package / 'program').write_bytes(b'Mac prepared program')
            runtime_input(package / 'romfs-inputs.zip')
            (package / 'generated').mkdir()
            for name in macos.HEADERS:
                (package / 'generated' / name).write_text('fixture table\n')
            config = {'nso_sha256': {'main': 'a' * 64}}
            info = {'format': 'd2r-mobile-macos-prepared', 'schema_version': 2,
                    'source_commit': commit, 'nso_sha256': config['nso_sha256'],
                    'input_revision': 'raw-archive', 'toolchain': 'producer-xcode',
                    'key': macos.generation_key(upstream, 'raw-archive', 'producer-xcode'),
                    'files': {p.relative_to(package).as_posix(): ci.sha256(p)
                              for p in package.rglob('*') if p.is_file()}}
            (package / 'manifest.json').write_text(json.dumps(info))
            archive = root / '.macos-downloads/prepared-input.archive'
            archive.parent.mkdir()
            with zipfile.ZipFile(archive, 'w') as z:
                for path in package.rglob('*'):
                    if path.is_file():
                        z.write(path, path.relative_to(package))
            before = archive.read_bytes()
            with patch.object(ci, 'ROOT', root), patch.object(ci, 'CONFIG', config):
                (upstream / 'dm/runtime.c').write_text('updated native runtime\n')
                macos.import_prepared()
                self.assertEqual((root / '.macos-prepared/program').read_bytes(), b'Mac prepared program')
                (upstream / 'dm/ios/tools/gen_assets.py').write_text('updated_interface = True\n')
                macos.import_prepared()
                (upstream / 'dm/tools/helper.py').write_text('changed_generation = True\n')
                with self.assertRaisesRegex(ValueError, 'changed generation inputs'):
                    macos.import_prepared()
            self.assertEqual(archive.read_bytes(), before)

    def test_mac_release_checks_both_packages_before_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / 'outputs'
            out.mkdir()
            config = {'upstream_commit': 'a' * 40, 'build_label': 'v1.2.3',
                      'build_number': '5.2', 'release_tag': 'v1.2.3-build.5.2'}
            report = {'source_commit': 'a' * 40, 'portable': True,
                      'native_built_from_source': True, 'release_tag': config['release_tag'] + '-macos', 'packages': []}
            for minimum in ('26.0', '15.0'):
                name = f'D2RMobile-{config["release_tag"]}-macos{minimum.split(".")[0]}.zip'
                body = name.encode()
                (out / name).write_bytes(body)
                report['packages'].append({'name': name, 'minimum_os': minimum,
                                           'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)})
            (out / 'macos-build-report.json').write_text(json.dumps(report))
            (out / 'Mac-SHA256SUMS.txt').write_text('fixture')
            with patch.object(ci, 'ROOT', root), patch.object(ci, 'CONFIG', config), patch.dict(os.environ, GITHUB_REPOSITORY='owner/private'), patch.object(macos, 'publish_assets') as publish:
                macos.publish()
                self.assertEqual(len(publish.call_args.args[-1]), 4)
                self.assertIn('## 简体中文', (out / 'MAC-RELEASE.md').read_text())
                publish.reset_mock()
                (out / report['packages'][0]['name']).write_bytes(b'damaged')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    macos.publish()
                publish.assert_not_called()
