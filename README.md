# ascend-image-ci

Orchestration repo for building Ascend/NPU container images on demand. It holds
only CI definitions — the Dockerfiles and source live in their upstream repos
and are checked out at build time.

Builds two images, multi-arch (`linux/amd64` + `linux/arm64`), and pushes
multi-arch manifests to `quay.io/fayeomni/*`:

| Image | Source repo | Dockerfiles | Tag scheme |
|-------|-------------|-------------|------------|
| `vllm-ascend` | [`vllm-project/vllm-ascend`](https://github.com/vllm-project/vllm-ascend) | `Dockerfile`, `Dockerfile.a3`, `Dockerfile.310p`, `Dockerfile.a5` | `<vllm_tag>-<ascend_ref>[-a3|-310p|-a5]` |
| `vllm-omni` | [`vllm-project/vllm-omni`](https://github.com/vllm-project/vllm-omni) | `docker/Dockerfile.npu`, `docker/Dockerfile.npu.a3` | `<vllm_tag>-<ascend_ref>-omni-<omni_ref>[-a3]` |

### Temporary: omni 310p/A5 images

Upstream vllm-omni has no 310p/a5 NPU Dockerfiles yet, so temporary ones live
in this repo (`docker/vllm-omni/Dockerfile.npu.ci.310p`,
`docker/vllm-omni/Dockerfile.npu.ci.a5`) and clone vllm-omni at build time.
The temporary **Build Omni 310p/A5 images (temporary)** workflow
(`build_omni_310p_a5_images.yaml`) builds them FROM the matching vllm-ascend
variant tags and publishes
`<vllm_ascend_base_tag>-omni-<omni_ref>-310p` / `-a5`. Once upstream ships
`docker/Dockerfile.npu.310p` / `.a5`, delete that workflow and those
Dockerfiles and fold the variants into the omni matrix in
`build_images.yaml`.

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
- `.github/workflows/_build_push_image.yaml` — reusable per-image multi-arch build + manifest merge
