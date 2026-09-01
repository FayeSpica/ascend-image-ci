# ascend-image-ci

## Model matrix

The repository publishes the Omni hardware model matrix with GitHub Pages. After
the Pages deployment workflow has run, open
`https://fayespica.github.io/ascend-image-ci/`.

Orchestration repo for building Ascend/NPU container images on demand. It holds
only CI definitions — the Dockerfiles and source live in their upstream repos
and are checked out at build time.

Builds two images, multi-arch (`linux/amd64` + `linux/arm64`), and pushes
multi-arch manifests to `quay.io/fayeomni/*`:

| Image | Source repo | Dockerfiles | Tag scheme |
|-------|-------------|-------------|------------|
| `vllm-ascend` | [`vllm-project/vllm-ascend`](https://github.com/vllm-project/vllm-ascend) | `Dockerfile`, `Dockerfile.a3`, `Dockerfile.310p`, `Dockerfile.a5` | `<vllm_tag>-<ascend_ref>[-a3|-310p|-a5]` |
| `vllm-omni` | [`vllm-project/vllm-omni`](https://github.com/vllm-project/vllm-omni) | `docker/vllm-omni/Dockerfile.npu` | `<omni_tag>[-a3|-a5|-310p]` |

### Publish an Omni release

Use **Build Omni images** to publish all supported variants from one vLLM-Omni
ref. The workflow builds native `linux/amd64` and `linux/arm64` images for A2,
A3, A5, and 310P, then publishes one multi-arch manifest per variant.

```bash
gh workflow run build_omni_images.yaml --repo FayeSpica/ascend-image-ci \
  -f omni_ref=v0.28.0 \
  -f vllm_ascend_image=quay.io/fayeomni/vllm-ascend \
  -f vllm_ascend_base_tag=v0.28.0-pr14898-patch15321-mindiesd \
  -f omni_image=quay.io/fayeomni/vllm-omni \
  -f omni_tag=v0.28.0
```

This example publishes:

- `quay.io/fayeomni/vllm-omni:v0.28.0`
- `quay.io/fayeomni/vllm-omni:v0.28.0-a3`
- `quay.io/fayeomni/vllm-omni:v0.28.0-a5`
- `quay.io/fayeomni/vllm-omni:v0.28.0-310p`

`vllm_ascend_base_tag` must omit the hardware suffix. The workflow appends
no suffix for A2 and appends `-a3`, `-a5`, or `-310p` for the other variants.

## Dependency chain

`vllm-omni`'s NPU image is built `FROM` the `vllm-ascend` image
(`FROM ${VLLM_ASCEND_IMAGE}:${VLLM_ASCEND_TAG}`). When both are selected the
omni build runs after the ascend build and uses the freshly built tag as its
base. To build omni alone against an existing ascend image, select `omni` only
and set `vllm_ascend_base_tag`.

## Usage

Trigger **Build Ascend/Omni images** (manual / `workflow_dispatch`):

```bash
# build both (omni FROM the just-built ascend image)
gh workflow run build_images.yaml \
  -f targets=ascend+omni \
  -f vllm_tag=v0.23.0 \
  -f ascend_ref=main \
  -f omni_ref=main

# build only ascend
gh workflow run build_images.yaml -f targets=ascend -f vllm_tag=v0.23.0 -f ascend_ref=main

# build only omni against an existing ascend tag
gh workflow run build_images.yaml \
  -f targets=omni -f omni_ref=main \
  -f vllm_ascend_base_tag=v0.23.0-main
```

### Temporary patch

Optionally apply one or more vllm-ascend PRs to the checked-out source before
building. Pass comma-separated PR numbers; each is fetched from
`https://github.com/vllm-project/vllm-ascend/pull/<n>.diff` and applied with
`git apply`. The build fails loudly if a patch does not apply cleanly.
Patched builds publish under a `-patch<N>` tag fragment so they don't
clobber the pristine `<vllm_tag>-<ascend_ref>` image.

```bash
# build vllm-ascend at a PR ref with an extra temporary patch (PR 15321)
gh workflow run build_images.yaml \
  -f targets=ascend \
  -f vllm_tag=v0.28.0 \
  -f ascend_ref=refs/pull/14898/head \
  -f patch_refs=15321
```

## Required configuration

In repo `Settings → Secrets and variables → Actions`:

- **Variable** `QUAY_USERNAME` — Quay robot account, e.g. `fayeomni+ci`
- **Secret** `QUAY_PASSWORD` — that robot account's token

The robot account must have **Write** access to `quay.io/fayeomni/vllm-ascend`
and `quay.io/fayeomni/vllm-omni`. Source repos are public, so no checkout token
is needed (add a PAT to the reusable workflow's checkout step if you switch to
private/fork sources).

## Files

- `.github/workflows/build_images.yaml` — dispatcher / orchestrator
- `.github/workflows/build_omni_images.yaml` — publish all Omni hardware variants
- `.github/workflows/_build_push_image.yaml` — reusable per-image multi-arch build + manifest merge
