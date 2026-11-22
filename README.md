# imagenudge-lite: Docker image update checker for running containers, read-only

imagenudge-lite helps with a Docker image update checker for a quick "which of my containers are behind?" check on one Docker host: it lists the running containers through the local Docker socket, asks the registry whether each container's tag now points to a different image digest, and whether a newer version tag exists (for example `1.25.1` -> `1.26.0`). It prints a plain-text list and exits `1` when something can be updated, so it fits a cron job. It never pulls, restarts or edits anything. One file, Python 3.9+ standard library only, MIT.

Need compose-file scanning, webhook / ntfy / SMTP email alerts, or JSON output? That is the full [ImageNudge](https://py-forge.com/products/imagenudge-container-update-notifier?utm_source=github&utm_medium=readme&utm_campaign=imagenudge-lite) (one-time payment, yours to keep, no subscription). A table later in this README lists what lite does and what the paid tool adds. Or get every PyForge tool, current and future, with the [All-Access Pass](https://py-forge.com/pass?utm_source=github&utm_medium=readme&utm_campaign=imagenudge-lite) ($99, one payment).

- Optional `pip install`, or just download the one file. No config, no state, no agent to run.
- Only `GET` requests to the Docker socket (`/containers/json`, `/images/<id>/json`). Registry checks are `HEAD` on the manifest plus the tag list; no image layers are downloaded.
- Public images only, anonymous access (Docker Hub, and registries that hand out anonymous pull tokens, e.g. public `ghcr.io` and `quay.io` images). No credentials are read or sent.
- Flags: `--socket`, `--no-tags`, `--demo`, `--timeout`, `--version`. After `pip install`, use `imagenudge-lite` in place of `python3 imagenudge_lite.py`.

<!-- TODO: terminal GIF of `--demo` or a live run is still missing. Place it under the intro, before Install. Do not invent a GIF. -->

## Install

```bash
pip install imagenudge-lite
imagenudge-lite --help
```

Or download the single file (Python 3.9+, stdlib only, no extra packages):

```bash
curl -fsSLO https://raw.githubusercontent.com/alessio1999xx-debug/imagenudge-lite/main/imagenudge_lite.py
python3 imagenudge_lite.py --help
```

## See the output first (offline demo)

```bash
python3 imagenudge_lite.py --demo
# imagenudge-lite 0.1.0: demo (sample data, no Docker, no network)
#
#   UPDATE  gitea        gitea/gitea:1.21.0  newer tag 1.21.3
#   UPDATE  redis-cache  redis:7.2-alpine    new digest for :7.2-alpine (b2b2b2b2b2b2 -> d4d4d4d4d4d4); newer tag 7.4-alpine
#   OK      traefik      traefik:v2.10.4     digest matches :v2.10.4
#   SKIP    my-app       my-app:dev          no registry digest locally (built or loaded locally?)
#
# 4 container(s): 2 update(s) available, 1 up to date, 1 skipped, 0 could not be checked.
# Nothing was pulled or restarted. Review the changelog, then update when you choose.
```

## Check running Docker containers for newer images

```bash
python3 imagenudge_lite.py
# needs read access to /var/run/docker.sock (root, or a user in the docker group)

python3 imagenudge_lite.py --socket /run/user/1000/docker.sock   # rootless Docker
python3 imagenudge_lite.py --no-tags                             # digests only, skip the tag list
```

If `DOCKER_HOST` is set to a `unix://` path it is used as the default socket. TCP / SSH Docker hosts are not supported by the lite version.

## How a Docker image update is detected

| Status | Meaning |
| --- | --- |
| `UPDATE` | The registry digest for the tag the container runs differs from the local image's digest (e.g. `:latest` or `:7.2-alpine` was rebuilt upstream), and/or a newer version tag exists |
| `OK` | Digest matches and no newer version tag was found |
| `SKIP` | Nothing to compare: container started from an image ID, pinned by digest only, or an image built/loaded locally with no registry digest |
| `ERROR` | The registry could not be checked (private or missing image, rate limit, no network). Shown with the reason |

Newer tags keep the same style and suffix as the tag you run: `1.25.1` is compared with `X.Y.Z` tags, `16-alpine` with `N-alpine`, `v2.10.4` with `vX.Y.Z`. Tags like `latest` or `stable` only get the digest check. The lite version reads up to 3 pages (3,000 tags) of the tag list, so on repositories with huge tag lists the "newer tag" suggestion can be lower than the real newest one. Multi-arch images are compared by their index digest, which is what `docker pull` by tag records locally.

## Exit codes for cron

| Exit code | Meaning |
| --- | --- |
| `0` | Every image that could be checked is up to date (or no running containers) |
| `1` | At least one container has an update available |
| `2` | Docker socket not reachable, bad arguments, or no image could be checked at all |

```bash
# crontab: check every morning at 07:00, keep the last report
0 7 * * * python3 /opt/imagenudge_lite.py > /var/log/imagenudge-lite.txt 2>&1
```

Scheduling is up to you (cron, a systemd timer). The lite version does not send any alerts.

## imagenudge-lite vs ImageNudge

imagenudge-lite checks the running containers on one local Docker socket and prints a plain-text list. That's it.

The full [ImageNudge](https://py-forge.com/products/imagenudge-container-update-notifier?utm_source=github&utm_medium=readme&utm_campaign=imagenudge-lite) adds compose-file scanning, webhook / ntfy-style and SMTP email alerts, JSON output and a printed list of the `docker pull` commands to run yourself. One-time payment, yours to keep. No subscription.

Lite is not a strict subset: its `0` / `1` / `2` exit codes for cron are not in the paid tool.

| Capability | Lite (free) | Paid ImageNudge |
| --- | --- | --- |
| Running containers via the local Docker socket | Yes | Yes |
| `docker-compose.yml` files (`--compose-file`, several at once) | No | Yes |
| New digest for the same tag | Yes | Yes |
| Newer semver tag (same suffix and style) | Yes (up to 3 tag-list pages) | Yes (up to 5 tag-list pages) |
| Registries | Public images, anonymous access | Public images, anonymous access |
| Alerts | No | Webhook / ntfy-compatible POST (`--notify-url`) and SMTP email (`--email-to`) |
| Output | Plain text list | Text or JSON (`--format json`) |
| Prints the `docker pull` commands to run (`--apply`, never runs them) | No | Yes |
| Exit code `1` when updates are available | Yes | No (exits `0`) |
| Offline `--demo` | Yes | Yes, plus a built-in `--self-test` |

Neither version pulls images, restarts containers, edits files, or reads registry credentials.

Or get every PyForge tool, current and future, with the [All-Access Pass](https://py-forge.com/pass?utm_source=github&utm_medium=readme&utm_campaign=imagenudge-lite) ($99, one payment).

## Tests

```bash
python3 test_imagenudge_lite.py
```

The tests are offline: a fake registry, and a tiny fake Docker API on a temporary unix socket. No Docker and no network needed.

## More free tools from the same author

- [agentconfig-audit-lite](https://github.com/alessio1999xx-debug/agentconfig-audit-lite) — Audit MCP config for secrets: plaintext tokens and unpinned npx/uvx servers in `.mcp.json` and `CLAUDE.md`.
- [agenttrail-scrub-lite](https://github.com/alessio1999xx-debug/agenttrail-scrub-lite) — Claude Code secret scanner: find API keys and tokens leaked in AI agent transcripts.
- [cronpulse-lite](https://github.com/alessio1999xx-debug/cronpulse-lite) — Cron job monitoring in one Python file: alerts to Slack/Discord/webhook on failure or hang.
- [depcooldown-lite](https://github.com/alessio1999xx-debug/depcooldown-lite) — Dependency cooldown check: fail CI when a pinned PyPI or npm package is newer than N days.
- [homelab-doctor-lite](https://github.com/alessio1999xx-debug/homelab-doctor-lite) — Read-only homelab health check: stale images, exposed ports, expiring certs, silent cron, disk.
- [pthguard-lite](https://github.com/alessio1999xx-debug/pthguard-lite) — Scan Python site-packages for malicious `.pth` files and startup backdoors.
- [pulseping-lite](https://github.com/alessio1999xx-debug/pulseping-lite) — Check SSL certificate expiry and URL/port uptime from cron.
- [secenv-lite](https://github.com/alessio1999xx-debug/secenv-lite) — Find `.env` files committed to git or missing from `.gitignore`, plus obvious API keys inside.

## License

MIT (this lite version). The full ImageNudge is sold separately under its own license.
