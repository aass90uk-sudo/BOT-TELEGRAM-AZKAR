---
name: Git push workaround for this sandbox
description: Plain `git push origin main` hangs here; use GITHUB_PAT HTTPS URL instead.
---

In this Replit sandbox, a plain `git push origin main` hangs/times out because there's no interactive credential prompt available.

**Why:** No credential helper is configured for the `origin` remote's default auth flow in this environment.

**How to apply:** Push using an HTTPS URL with the `GITHUB_PAT` env var embedded directly:
`git push "https://${GITHUB_PAT}@github.com/<owner>/<repo>" main`
Pipe output through `sed "s/${GITHUB_PAT}/***/g"` to avoid leaking the token in logs.
Verify success with `git --no-optional-locks ls-remote https://github.com/<owner>/<repo> main` (read-only). Avoid running `git fetch`, which touches local remote-tracking refs and is blocked as destructive in this sandbox.
This pattern only works from a background Project Task context — the main agent itself cannot run commit/push commands directly.
