"""Resolve and promote nightly images; publication starts only after all checks pass."""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

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
    date = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d')
    tag = f"nightly-{date}-{sha[:12]}-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
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


def publish():
    tag = os.environ['CANDIDATE_TAG']
    sha = os.environ['OMNI_SHA']
    src_auth, dst_auth = os.environ['SOURCE_AUTH'], os.environ['DEST_AUTH']
    records = []
    # Complete this entire loop before any write to the destination registry.
    for key, suffix in VARIANTS:
        ref = f'{SOURCE}:{tag}{suffix}'
        digest = inspect(ref, src_auth, revision=sha)
        records.append((key, suffix, digest))
        summary(f'Candidate {key}: `{ref}` → `{digest}`')

    # All immutable copies must verify before the first rolling tag is changed.
    for rolling in (False, True):
        for key, suffix, digest in records:
            target = f"{DEST}:{'nightly' if rolling else tag}{suffix}"
            summary(f'Copy starting: `{SOURCE}@{digest}` → `{target}`')
            run('skopeo', 'copy', '--all', '--preserve-digests',
                '--src-authfile', src_auth, '--dest-authfile', dst_auth,
                f'docker://{SOURCE}@{digest}', f'docker://{target}')
            # If verification fails after a successful copy, record that distinction.
            summary(f'Copy completed; verification pending: `{target}`')
            actual = inspect(target, dst_auth, revision=sha)
            if actual != digest:
                raise ValueError(f'{target}: expected {digest}, got {actual}')
            summary(f'Published and verified: `{target}` → `{digest}`')


if __name__ == '__main__':
    try:
        {'prepare': prepare, 'publish': publish}[sys.argv[1]]()
    except Exception as exc:
        summary(f'FAILED: {exc}. See the last copy/verification entry for partial publication state.')
        raise
