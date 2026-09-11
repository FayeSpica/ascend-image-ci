"""Resolve and promote nightly images; publication starts only after all checks pass."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
import base64
import time
import hashlib
import json
import os
import re
import subprocess
import sys

SOURCE = 'quay.io/fayeomni/vllm-omni'
DEST = 'quay.io/ascend/vllm-omni'
VARIANTS = [('omni-a2', ''), ('omni-a3', '-a3'), ('omni-a5', '-a5'), ('omni-310p', '-310p')]


def run(*args):
    if args[:2] == ("skopeo", "copy"):
        subprocess.check_call(args)
        return b""
    return subprocess.check_output(args)


def summary(message):
    print(message, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(message + '\n\n')


def output(name, value):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(f'{name}={value}\n')


def inspect(ref, auth, revision=None):
    raw = run('skopeo', 'inspect', '--authfile', auth, '--raw', f'docker://{ref}')
    manifest = json.loads(raw)
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    children = manifest.get('manifests', [])
    platforms = {(m.get('platform', {}).get('os'), m.get('platform', {}).get('architecture')) for m in children}
    if not {('linux', 'amd64'), ('linux', 'arm64')} <= platforms:
        raise ValueError(f'{ref}: missing linux/amd64 or linux/arm64')
    if '@sha256:' in ref and ref.rsplit('@', 1)[1] != digest:
        raise ValueError(f'{ref}: manifest digest mismatch')
    if revision:
        repo = ref.split('@')[0].rsplit(':', 1)[0]
        for child in children:
            platform = child.get('platform', {})
            if (platform.get('os'), platform.get('architecture')) not in {('linux', 'amd64'), ('linux', 'arm64')}:
                continue
            config = json.loads(run('skopeo', 'inspect', '--authfile', auth, '--config',
                                    f"docker://{repo}@{child['digest']}"))
            labels = config.get('config', {}).get('Labels') or {}
            if labels.get('org.opencontainers.image.revision') != revision:
                raise ValueError(f'{ref}: {platform} revision mismatch')
    return digest


def prepare():
    sha = run('git', 'ls-remote', 'https://github.com/vllm-project/vllm-omni.git',
              'refs/heads/main').decode().split()
    if len(sha) != 2 or not re.fullmatch('[0-9a-f]{40}', sha[0]) or sha[1] != 'refs/heads/main':
        raise ValueError('Cannot resolve upstream main to one full commit SHA')
    sha = sha[0]
    tag = 'nightly'
    base_image = os.environ.get('BASE_IMAGE') or 'quay.io/atlas-ci/vllm-ascend'
    base_tag = os.environ.get('BASE_TAG') or 'v0.28.0'
    # These values enter a newline-separated Docker build-args input.
    if not re.fullmatch(r'[a-z0-9][a-z0-9._/-]*', base_image):
        raise ValueError('Invalid base image repository')
    if not re.fullmatch(r'[\w][\w.-]{0,127}', base_tag, flags=re.ASCII):
        raise ValueError('Invalid base tag')
    matrix = []
    summary(f"Omni SHA: `{sha}`; recipe SHA: `{os.environ['GITHUB_SHA']}`; candidate: `{tag}`")
    for key, suffix in VARIANTS:
        ref = f'{base_image}:{base_tag}{suffix}'
        digest = inspect(ref, os.environ['SOURCE_AUTH'])
        matrix.append(dict(key=key, suffix=suffix, base=f'{base_image}@{digest}',
                           patch='1' if suffix == '-310p' else '0'))
        summary(f'Base {key}: `{ref}` → `{digest}`')
    output('release_tag', f"nightly-{datetime.now(ZoneInfo('Asia/Shanghai')):%Y%m%d}-{sha[:7]}")
    output('sha', sha)
    output('tag', tag)
    output('matrix', json.dumps({'include': matrix}, separators=(',', ':')))


def publish():
    tag = os.environ['CANDIDATE_TAG']
    sha = os.environ['OMNI_SHA']
    src_auth, dst_auth = os.environ['SOURCE_AUTH'], os.environ['DEST_AUTH']
    release_tag = os.environ['RELEASE_TAG']
    if not re.fullmatch(r'nightly-\d{8}-' + re.escape(sha[:7]), release_tag):
        raise ValueError('Invalid release tag')
    records = []
    # Complete this entire loop before any write to the destination registry.
    for key, suffix in VARIANTS:
        ref = f'{SOURCE}:{tag}{suffix}'
        digest = inspect(ref, src_auth, revision=sha)
        records.append((key, suffix, digest))
        summary(f'Candidate {key}: `{ref}` → `{digest}`')

    # Preserve every dated image before advancing any rolling aliases.
    for tag_base in (release_tag, 'nightly'):
        for key, suffix, digest in records:
            target = f"{DEST}:{tag_base}{suffix}"
            summary(f'Copy starting: `{SOURCE}@{digest}` → `{target}`')
            run('skopeo', 'copy', '--all', '--preserve-digests',
                '--src-authfile', src_auth, '--dest-authfile', dst_auth,
                f'docker://{SOURCE}@{digest}', f'docker://{target}')
            actual = inspect(target, dst_auth, revision=sha)
            if actual != digest:
                raise ValueError(f'{target}: expected {digest}, got {actual}')
            summary(f'Published and verified: `{target}` → `{digest}`')


def inventory():
    tags = {}
    page = 1
    while True:
        url = ('https://quay.io/api/v1/repository/ascend/vllm-omni/'
               f'tag/?onlyActiveTags=true&limit=100&page={page}')
        with urlopen(url, timeout=60) as response:
            result = json.load(response)
        tags.update({tag['name']: tag['manifest_digest'] for tag in result['tags']})
        if not result['has_additional']:
            return tags
        page += 1


def registry_token():
    user = os.environ.get('ASCEND_QUAY_USERNAME')
    password = os.environ.get('ASCEND_QUAY_PASSWORD')
    if not user or not password:
        raise ValueError('Ascend robot credentials are required for history cleanup')
    basic = base64.b64encode(f'{user}:{password}'.encode()).decode()
    request = Request('https://quay.io/v2/auth?service=quay.io&scope=repository:ascend/vllm-omni:*',
                      headers={'Authorization': 'Basic ' + basic})
    with urlopen(request, timeout=60) as response:
        return json.load(response)['token']


def delete_tag(name, token):
    request = Request(f'https://quay.io/v2/ascend/vllm-omni/manifests/{name}',
                      headers={'Authorization': 'Bearer ' + token}, method='DELETE')
    with urlopen(request, timeout=60) as response:
        if response.status != 202:
            raise ValueError(f'{name}: unexpected delete status {response.status}')


def cleanup():
    # Keep today and the previous 13 calendar days in Asia/Shanghai.
    cutoff = datetime.now(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=13)
    before = inventory()
    expired = set()
    for name in before:
        match = re.fullmatch(r'nightly-(\d{8})-[0-9a-f]{7}(?:-a3|-a5|-310p)?', name)
        if not match:
            continue
        try:
            date = datetime.strptime(match[1], '%Y%m%d').date()
        except ValueError:
            continue
        if date < cutoff:
            expired.add(name)
    if not expired:
        summary('No expired history tags.')
        return
    token = registry_token()
    expected = dict(before)
    for name in sorted(expired):
        if inventory() != expected:
            raise ValueError('Tag inventory changed before deletion; stopping cleanup')
        delete_tag(name, token)
        summary(f'Deleted expired history tag: `{DEST}:{name}`')
        del expected[name]
        for attempt in range(5):
            if inventory() == expected:
                break
            if attempt == 4:
                raise ValueError('Post-delete inventory mismatch; stopping cleanup')
            time.sleep(2)
    summary('Verified: only expired tags removed; all retained tags and digests unchanged.')


if __name__ == '__main__':
    try:
        {'prepare': prepare, 'publish': publish, 'cleanup': cleanup}[sys.argv[1]]()
    except Exception as exc:
        summary(f'FAILED: {exc}. See the last copy/verification entry for partial publication state.')
        raise
