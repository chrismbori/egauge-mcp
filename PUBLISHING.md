# Publishing to PyPI

The repo has a GitHub Actions workflow that publishes to PyPI on every `v*` tag push. **One-time setup is required on PyPI.org** because GitHub Actions needs to authenticate as a Trusted Publisher.

## One-time PyPI setup

1. Create a PyPI account at https://pypi.org if you don't have one.
2. Go to https://pypi.org/manage/account/publishing/ and click **Add a new pending publisher**.
3. Fill in:
   - **PyPI Project Name:** `egauge-mcp`
   - **Owner:** `chrismbori`
   - **Repository name:** `egauge-mcp`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
4. Save.

That's it — no API token to manage, no secrets to leak.

## Publishing a release

```bash
# Bump version in pyproject.toml first, then:
git tag v0.1.0
git push origin v0.1.0
```

The workflow builds + publishes automatically. After ~2 minutes the package is live at:
- https://pypi.org/project/egauge-mcp/
- Installable via `uvx egauge-mcp` or `pip install egauge-mcp`

## Manual publish (if you skip the GitHub setup)

```bash
uv build
uv publish --token pypi-AgEI...   # generate from https://pypi.org/manage/account/token/
```
