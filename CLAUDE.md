# CLAUDE.md

## Project

styx — graceful cluster shutdown orchestrator for Proxmox + Kubernetes + Ceph.

## Tests

```bash
python3 -m unittest discover -s test -q
```

## Releasing

CI (`.github/workflows/release.yml`) runs on tag push and verifies that the tag matches both `styx/__init__.py:__version__` and `scripts/install.sh:VERSION`. To prepare a release:

1. Bump `__version__` in `styx/__init__.py` to match the new version (e.g. `'0.2.0'`)
2. Bump `VERSION` in `scripts/install.sh` to match (e.g. `"0.2.0"`)
3. Commit the version bump
4. Tag and push: `git tag v0.2.0 && git push && git push --tags`
5. Create the GitHub release: `gh release create v0.2.0 --generate-notes`

CI will build `styx.pyz`, run tests, and upload `styx.pyz` + `install.sh` as release assets. Do **not** create the GitHub release before the version bump commit is tagged — the `softprops/action-gh-release` step in CI will create/update the release with assets automatically.
