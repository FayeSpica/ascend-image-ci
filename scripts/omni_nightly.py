"""Resolve and promote nightly images; publication starts only after all checks pass."""

import base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import urllib.request
import urllib.parse

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
    tag = f'nightly-{today():%Y%m%d}-{sha[:7]}'
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
    output('sha', sha)
    output('tag', tag)
    output('matrix', json.dumps({'include': matrix}, separators=(',', ':')))


def today():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date()


def expired_tags(tags, current_date):
    # Keep today and the previous 13 calendar dates, including staging tags.
    cutoff = current_date - timedelta(days=13)
    expired = []
    for tag in tags:
        match = re.fullmatch(r'nightly-(\d{8})-[0-9a-f]{7}(?:-a3|-a5|-310p)?(?:-amd64|-arm64)?', tag)
        if match:
            try:
                date = datetime.strptime(match[1], '%Y%m%d').date()
            except ValueError:
                continue
            if date < cutoff:
                expired.append(tag)
    return sorted(expired)


class Registry:
    def __init__(self, image, username, password):
        self.repo = image.removeprefix('quay.io/')
        if not image.startswith('quay.io/') or not username or not password:
            raise ValueError('Quay repository and credentials required')
        basic = base64.b64encode(f'{username}:{password}'.encode()).decode()
        query = urllib.parse.urlencode({'service': 'quay.io', 'scope': f'repository:{self.repo}:*'})
        req = urllib.request.Request('https://quay.io/v2/auth?' + query,
                                     headers={'Authorization': 'Basic ' + basic})
        with urllib.request.urlopen(req, timeout=60) as response:
            self.token = json.load(response)['token']

    def inventory(self):
        tags = {}
        page = 1
        while True:
            req = urllib.request.Request(
                f'https://quay.io/api/v1/repository/{self.repo}/tag/?limit=100&onlyActiveTags=true&page={page}')
            with urllib.request.urlopen(req, timeout=60) as response:
                data = json.load(response)
            tags.update({tag['name']: tag['manifest_digest'] for tag in data['tags']})
            if not data['has_additional']:
                return tags
            page += 1

    def delete_tag(self, tag):
        # Quay's tag route deletes only the pointer, unlike DELETE by digest.
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag):
            raise ValueError('Invalid tag')
        req = urllib.request.Request(f'https://quay.io/v2/{self.repo}/manifests/{tag}', method='DELETE',
                                     headers={'Authorization': 'Bearer ' + self.token})
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.status not in (202, 204):
                raise ValueError(f'Delete {tag}: HTTP {response.status}')
        summary(f'Deleted tag: `{self.repo}:{tag}`')


def cleanup(image, username, password, staging=()):
    registry = Registry(image, username, password)
    before = registry.inventory()
    targets = set(expired_tags(before, today())) | (set(staging) & before.keys())
    for tag in sorted(targets):
        registry.delete_tag(tag)
    after = registry.inventory()
    if after != {tag: digest for tag, digest in before.items() if tag not in targets}:
        raise ValueError(f'{image}: cleanup inventory mismatch')
    summary(f'Cleanup verified: `{image}`; removed {len(targets)} tags; all other tag digests unchanged.')


def publish():
    tag = os.environ['CANDIDATE_TAG']
    sha = os.environ['OMNI_SHA']
    if not re.fullmatch(r'nightly-\d{8}-[0-9a-f]{7}', tag) or tag[-7:] != sha[:7]:
        raise ValueError('Invalid dated candidate tag or SHA mismatch')
    src_auth = os.environ['SOURCE_AUTH']
    records = []
    for key, suffix in VARIANTS:
        ref = f'{SOURCE}:{tag}{suffix}'
        digest = inspect(ref, src_auth, revision=sha)
        records.append((key, suffix, digest))
        summary(f'Candidate {key}: `{ref}` → `{digest}`')

    destinations = [(SOURCE, src_auth, False)]
    if os.environ.get('RETAG_TO_ASCEND', 'false') == 'true':
        destinations.append((DEST, os.environ['DEST_AUTH'], True))
    for image, auth, include_dated in destinations:
        for key, suffix, digest in records:
            tags = [f'{tag}{suffix}', f'nightly{suffix}'] if include_dated else [f'nightly{suffix}']
            for target_tag in tags:
                target = f'{image}:{target_tag}'
                summary(f'Copy starting: `{SOURCE}@{digest}` → `{target}`')
                run('skopeo', 'copy', '--all', '--preserve-digests',
                    '--src-authfile', src_auth, '--dest-authfile', auth,
                    f'docker://{SOURCE}@{digest}', f'docker://{target}')
                summary(f'Copy completed; verification pending: `{target}`')
                actual = inspect(target, auth, revision=sha)
                if actual != digest:
                    raise ValueError(f'{target}: expected {digest}, got {actual}')
                summary(f'Published and verified: `{target}` → `{digest}`')

    staging = [f'{tag}{suffix}-{arch}' for _, suffix in VARIANTS for arch in ('amd64', 'arm64')]
    cleanup(SOURCE, os.environ['QUAY_USER'], os.environ['QUAY_PASS'], staging)
    if os.environ.get('RETAG_TO_ASCEND', 'false') == 'true':
        cleanup(DEST, os.environ['ASCEND_USER'], os.environ['ASCEND_PASS'])


if __name__ == '__main__':
    try:
        {'prepare': prepare, 'publish': publish}[sys.argv[1]]()
    except Exception as exc:
        summary(f'FAILED: {exc}. See the last copy/verification entry for partial publication state.')
        raise
