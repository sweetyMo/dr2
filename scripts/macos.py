"""Build portable Apple silicon apps using upstream's mac_app.sh."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci
from archive_input import extract
from input_config import input_identity, settings
from source.compatibility import generation_paths, verify_generation
from source.game_input import download, unpack
from source.publish import publish_assets
from source.prepared import download as download_prepared, resolve as plan_prepared
from source.romfs_input import create

HEADERS = ('dm_vault_tab.h', 'dm_hud_exp.h', 'dm_bright_levels.inc')


def run(*args, **kwargs):
    return subprocess.check_output(list(map(str, args)), text=True, **kwargs).strip()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def generation_key(upstream, identity, toolchain):
    paths = generation_paths(upstream, None)
    paths.update(('setup.sh', 'dm/tools/product.sh',
                  'dm/gfx/tools/msl_retarget.sh', 'dm/ios/tools/gen_vault_table.py',
                  'dm/ios/tools/gen_hud_exp.py', 'dm/tools/gen_bright_levels.py'))
    digest = hashlib.sha256((identity + '\0' + toolchain).encode())
    for name in sorted(paths):
        path = upstream / name
        if path.is_file():
            digest.update((name + '\0' + ci.sha256(path) + '\0').encode())
    digest.update(Path(__file__).read_bytes())
    digest.update((Path(__file__).parent / 'source/romfs_input.py').read_bytes())
    return 'macos-prepared-v1-' + digest.hexdigest()


def plan():
    url, digest = settings('GAME_ARCHIVE')
    identity = input_identity(url, digest)
    key = generation_key(ci.ROOT / 'upstream', identity, os.environ['MACOS_TOOLCHAIN'])
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write('input_revision=' + identity + '\nprepared_key=' + key + '\n')


def load_prepared():
    cache = ci.ROOT / '.macos-prepared'
    info = json.loads((cache / 'manifest.json').read_text())
    if info.get('format') != 'd2r-mobile-macos-prepared' or info.get('schema_version') != 2:
        raise ValueError('Expected a macOS prepared package (schema 2)')
    if not {'romfs-inputs.zip', *('generated/' + n for n in HEADERS)} <= info['files'].keys():
        raise ValueError('Mac prepared package is missing original inputs or game tables')
    expected = os.environ.get('MACOS_PREPARED_KEY')
    if expected is None:
        verify_generation(ci.ROOT / 'upstream', info['source_commit'])
        expected = info['key']
    if info['key'] != expected:
        raise ValueError('Mac preparation cache belongs to different inputs')
    files = {p.relative_to(cache).as_posix() for p in cache.rglob('*') if p.is_file()}
    if files != set(info['files']) | {'manifest.json'}:
        raise ValueError('Mac preparation cache is incomplete')
    for name, digest in info['files'].items():
        if (cache / name).is_symlink() or ci.sha256(cache / name) != digest:
            raise ValueError('Mac preparation cache checksum mismatch')
    return info


def verify():
    try:
        load_prepared()
        reused = True
    except (OSError, ValueError, KeyError, TypeError):
        reused = False
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write('reused=' + str(reused).lower() + '\n')
    print('Reused verified Mac inputs:', reused, flush=True)


def prepare():
    root = ci.ROOT
    upstream = root / 'upstream'
    dm = upstream / 'dm'
    workspace = root / '.macos-work'
    archive = root / '.game-downloads/game-input.archive'
    game = unpack(workspace)
    started = time.monotonic()
    environment = dict(os.environ, DM_ROOT=str(workspace))
    subprocess.run(['bash', str(upstream / 'setup.sh')], env=environment, check=True)
    python = workspace / 'build/cache/dm/venv/bin/python3'
    cache = root / '.macos-prepared'
    stage = root / '.macos-prepared.partial'
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    metadata = stage / 'resources/data'
    metadata.mkdir(parents=True)
    for name in ('build_manifest.psv', '.build.config'):
        shutil.copy2(game / 'RomFS/data' / name, metadata / name)
    product = (dm / 'PRODUCT').read_text().strip()
    shutil.copytree(workspace / 'build/cache/dm/out' / (product + '-touch'), stage / 'product',
                    ignore=shutil.ignore_patterns('*.log'))
    subprocess.run(['bash', str(dm / 'gfx/tools/msl_retarget.sh'), '15.0', str(stage / 'msl')],
                   env=environment, check=True)
    generators = ('ios/tools/gen_vault_table.py', 'ios/tools/gen_hud_exp.py',
                  'tools/gen_bright_levels.py')
    (stage / 'generated').mkdir()
    for name, script in zip(HEADERS, generators):
        with (stage / 'generated' / name).open('w') as output:
            subprocess.run([str(python), str(dm / script), str(game / 'RomFS')],
                           env=environment, stdout=output, check=True)
    shutil.copy2(dm / 'gfx/tools/dmsh_chain.lock', stage / 'shader-chain.lock')
    shutil.copy2(dm / 'gfx/nvn/c1_full.tsv', stage / 'c1_full.tsv')
    chain = run(python, dm / 'gfx/tools/dmsh_chain.py', '--header')
    libraries = list((stage / 'msl' / ('b-' + chain)).glob('*.metallib'))
    if not libraries or any(not p.read_bytes().startswith(b'MTLB') for p in libraries):
        raise ValueError('Mac Metal libraries are missing or uncompiled')
    size = create(game.parent, stage / 'romfs-inputs.zip')
    info = {'format': 'd2r-mobile-macos-prepared', 'schema_version': 2,
            'toolchain': os.environ['MACOS_TOOLCHAIN'], 'nso_sha256': ci.CONFIG['nso_sha256'],
            'key': os.environ['MACOS_PREPARED_KEY'], 'source_commit': ci.CONFIG['upstream_commit'],
            'input_revision': os.environ['INPUT_REVISION'], 'archive_sha256': settings('GAME_ARCHIVE')[1] or ci.sha256(archive),
            'product': product, 'shader_chain': chain, 'metal_libraries': len(libraries),
            'metal_minimum_os': '15.0', 'runtime_input_bytes': size,
            'prepare_seconds': round(time.monotonic() - started, 2),
            'files': {p.relative_to(stage).as_posix(): ci.sha256(p)
                      for p in sorted(stage.rglob('*')) if p.is_file()}}
    write(stage / 'manifest.json', info)
    shutil.rmtree(cache, ignore_errors=True)
    stage.rename(cache)
    print('Mac preparation completed and verified; original archive retained', flush=True)


def import_prepared():
    root = ci.ROOT
    target = root / '.macos-import'
    shutil.rmtree(target, ignore_errors=True)
    extract(root / '.macos-downloads/prepared-input.archive', target, max_members=100000)
    manifest = target / 'manifest.json'
    if manifest.stat().st_size > 8 * 1024**2:
        raise ValueError('Mac prepared manifest is too large')
    info = json.loads(manifest.read_text())
    if info['nso_sha256'] != ci.CONFIG['nso_sha256']:
        raise ValueError('Mac prepared package uses different game programs')
    cache = root / '.macos-prepared'
    shutil.rmtree(cache, ignore_errors=True)
    target.rename(cache)
    load_prepared()
    print('Mac prepared input verified; ready for native compilation', flush=True)


def export_prepared():
    root = ci.ROOT
    cache = root / '.macos-prepared'
    info = load_prepared()
    out = root / 'outputs'
    out.mkdir(exist_ok=True)
    package = out / 'D2RMobile-MacPrepared.zip'
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(cache.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(cache).as_posix())
    digest = ci.sha256(package)
    sums = out / 'MacPrepared-SHA256SUMS.txt'
    sums.write_text(digest + '  ' + package.name + '\n')
    body = out / 'MAC-PREPARED-RELEASE.md'
    body.write_text('macOS prepared input / macOS 预处理包\n\n'
                    f'Source / 源码：`{info["source_commit"]}`\n\n'
                    'Set its URL and SHA-256 in the prepared_input workflow parameter. / '
                    '将此 ZIP 的地址和校验值填入运行参数 prepared_input。\n')
    publish_assets(os.environ['GITHUB_REPOSITORY'], info['key'], 'macOS prepared input', body, [package, sums])
    print('Mac prepared archive SHA256:', digest, flush=True)


def build():
    root = ci.ROOT
    dm = root / 'upstream/dm'
    cache = root / '.macos-prepared'
    info = load_prepared()
    workspace = root / '.macos-runtime'
    from source.runtime_assets import generate
    generation_seconds = generate(dm, cache / 'romfs-inputs.zip', workspace, macos=True,
                                  headers=cache / 'generated', baseline=info['source_commit'])
    shutil.copyfile(cache / 'shader-chain.lock', dm / 'gfx/tools/dmsh_chain.lock')
    shutil.copyfile(cache / 'c1_full.tsv', dm / 'gfx/nvn/c1_full.tsv')
    if run(sys.executable, dm / 'gfx/tools/dmsh_chain.py', '--header') != info['shader_chain']:
        raise ValueError('Mac shader chain differs from the selected source')
    product = workspace / 'build/cache/dm/out' / (info['product'] + '-touch')
    shutil.rmtree(product, ignore_errors=True)
    shutil.copytree(cache / 'product', product)
    out = root / 'outputs'
    out.mkdir(exist_ok=True)
    environment = dict(os.environ, DM_ROOT=str(workspace), DM_MAC_LINK='0',
                       DM_MAC_OVERLAY=str(workspace / 'romfs_overlay'), DM_MSL_DIR=str(cache / 'msl'),
                       DM_MAC_VERSION=ci.CONFIG['app_version'], DM_MAC_BUILD=ci.CONFIG['build_number'],
                       MACOS_MIN='26.0', MACOS_MIN_M3='15.0')
    # Compile both upstream targets together; make shares header dependencies safely.
    started = time.monotonic()
    subprocess.run(['make', '-j2', '-C', str(dm / 'host'), 'dmhost', 'dmhost_m3'],
                   env=environment, check=True)
    packages = []
    for minimum, app_name, older in (('26.0', 'D2R Mobile.app', ''), ('15.0', 'D2R Mobile M3.app', '1')):
        subprocess.run(['bash', str(dm / 'tools/mac_app.sh'), str(out / 'macos-apps')],
                       env=dict(environment, DM_MAC_M3=older), check=True)
        app = out / 'macos-apps' / app_name
        plist = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        native = (app / 'Contents/MacOS' / plist['CFBundleExecutable']).read_bytes()
        version = ci.macho_minimum_os(native, platform=1)
        if (plist['LSMinimumSystemVersion'] != minimum or version != (int(minimum.split('.')[0]), 0, 0)
                or plist['CFBundleVersion'] != ci.CONFIG['build_number']
                or plist['CFBundleShortVersionString'] != ci.CONFIG['app_version']):
            raise ValueError('Mac app system or version differs from the requested build')
        if (app / 'Contents/Resources/paths.txt').exists() or any(p.is_symlink() for p in app.rglob('*')):
            raise ValueError('Mac app still references external build files')
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
        name = f'D2RMobile-{ci.release_tag()}-macos{minimum.split(".")[0]}.zip'
        path = out / name
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(path)], check=True)
        with zipfile.ZipFile(path, 'a', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for metadata in sorted((cache / 'resources/data').iterdir()):
                archive.write(metadata, 'Mac Resource Metadata/data/' + metadata.name)
        packages.append({'name': name, 'sha256': ci.sha256(path), 'bytes': path.stat().st_size,
                         'app_name': app_name, 'minimum_os': minimum,
                         'native_minimum_os': '.'.join(map(str, version)),
                         'native_sha256': hashlib.sha256(native).hexdigest(),
                         'bundle_identifier': plist['CFBundleIdentifier'], 'signature': 'ad-hoc'})
    report = {'platform': 'macos', 'source_repository': os.environ['SOURCE_REPOSITORY'],
              'source_commit': ci.CONFIG['upstream_commit'], 'tools_commit': os.environ['TOOLS_COMMIT'],
              'app_version': ci.CONFIG['app_version'], 'build_number': ci.CONFIG['build_number'],
              'release_tag': ci.release_tag() + '-macos', 'prepared_from_source': info['source_commit'],
              'input_revision': info['input_revision'], 'archive_sha256': info['archive_sha256'],
              'prepared_archive_sha256': ci.sha256(root / '.macos-downloads/prepared-input.archive'),
              'product': info['product'], 'shader_chain': info['shader_chain'],
              'metal_libraries': info['metal_libraries'], 'metal_minimum_os': info['metal_minimum_os'],
              'prepared_cache_hit': os.environ.get('MACOS_PREPARED_CACHE_HIT') == 'true',
              'native_build_seconds': round(time.monotonic() - started, 2),
              'runtime_generation_seconds': generation_seconds,
              'native_built_from_source': True, 'portable': True, 'device_tested': False,
              'xcode': run('xcodebuild', '-version'), 'sdk': run('xcrun', '--sdk', 'macosx', '--show-sdk-version'),
              'packages': packages}
    write(out / 'macos-build-report.json', report)
    (out / 'Mac-SHA256SUMS.txt').write_text(''.join(f"{p['sha256']}  {p['name']}\n" for p in packages))
    print(json.dumps(report, indent=2), flush=True)


def publish():
    repo = os.environ['GITHUB_REPOSITORY']
    out = ci.ROOT / 'outputs'
    report = json.loads((out / 'macos-build-report.json').read_text())
    if (report['source_commit'] != ci.CONFIG['upstream_commit'] or not report['portable']
            or not report['native_built_from_source'] or report['release_tag'] != ci.release_tag() + '-macos'):
        raise ValueError('Mac build report does not match this build')
    paths = []
    for item, minimum in zip(report['packages'], ('26.0', '15.0'), strict=True):
        expected = f'D2RMobile-{ci.release_tag()}-macos{minimum.split(".")[0]}.zip'
        path = out / expected
        if (item['name'] != expected or item['minimum_os'] != minimum
                or ci.sha256(path) != item['sha256'] or path.stat().st_size != item['bytes']):
            raise ValueError('Mac package or checksum mismatch')
        paths.append(path)
    body = out / 'MAC-RELEASE.md'
    body.write_text('## English\n\n'
                    f'Source: `{report["source_commit"]}`.\n\n'
                    '- `macos26.zip`: macOS 26 and later.\n- `macos15.zip`: macOS 15–25.\n\n'
                    'Extract the ZIP and move the app to Applications. On first launch, choose your game’s '
                    'D2R Mobile Resources folder after merging the included Mac Resource Metadata files into it. Metal libraries are precompiled. '
                    'This build has not been tested on a physical device. '
                    '[Usage](https://github.com/jiayx/d2r-mobile-build/blob/main/README.md#download-and-use-on-macos).\n\n'
                    '## 简体中文\n\n'
                    f'源码：`{report["source_commit"]}`。\n\n'
                    '- `macos26.zip`：macOS 26 及以上。\n- `macos15.zip`：macOS 15–25。\n\n'
                    '解压 ZIP，将应用移到“应用程序”。将附带的 Mac Resource Metadata 合并到 Data 文件夹，首次启动选择 D2R Mobile Resources。'
                    'Metal 着色器已预编译，未做真机测试。'
                    '[使用说明](https://github.com/jiayx/d2r-mobile-build/blob/main/README.zh-CN.md#macos-下载和使用)。\n')
    return publish_assets(repo, report['release_tag'],
                          f"{ci.build_label()} · macOS build {ci.CONFIG['build_number']}", body,
                          [*paths, out / 'Mac-SHA256SUMS.txt', out / 'macos-build-report.json'])


def main(command):
    {'plan': plan, 'download': download, 'prepare': prepare,
     'verify': verify, 'build': build, 'publish': publish,
     'plan-prepared': plan_prepared, 'import': import_prepared, 'export': export_prepared,
     'download-prepared': lambda: download_prepared(ci.ROOT, cache_name='.macos-downloads')}[command]()


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception as exc:
        print('::error::' + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
