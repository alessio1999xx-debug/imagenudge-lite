#!/usr/bin/env python3
"""imagenudge-lite: check running Docker containers for newer images (read-only).

Lists the running containers on the local Docker host (via the Docker socket),
then asks the registry whether the tag each container runs now points to a
different digest, and optionally whether a newer semver tag exists. Prints a
plain-text list. It never pulls, restarts or edits anything.

Registry access is anonymous only (public images). Python 3.9+ standard
library only. MIT license.

Exit codes: 0 = all checked images up to date, 1 = at least one update
available, 2 = error (Docker socket not reachable, bad arguments, or no image
could be checked at all).

Full version (compose files, webhook / ntfy / SMTP alerts, JSON output):
https://py-forge.com/products/imagenudge-container-update-notifier?utm_source=github&utm_medium=readme&utm_campaign=imagenudge-lite
"""

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

__version__ = "0.1.0"

DEFAULT_SOCKET = "/var/run/docker.sock"
USER_AGENT = "imagenudge-lite/" + __version__
PRO_URL = (
    "https://py-forge.com/products/imagenudge-container-update-notifier"
    "?utm_source=github&utm_medium=readme&utm_campaign=imagenudge-lite"
)
MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class DockerError(Exception):
    """The local Docker socket could not be read."""


# ---------------------------------------------------------------------------
# Image references and tags
# ---------------------------------------------------------------------------


class ImageRef:
    """A Docker image reference split into registry, repository, tag, digest."""

    def __init__(self, raw: str) -> None:
        self.raw = raw.strip()
        self.registry = "docker.io"
        self.repository = ""
        self.tag = "latest"
        self.digest = ""
        self.tag_given = False
        ref = self.raw
        if "@" in ref:
            ref, self.digest = ref.split("@", 1)
        first = ref.split("/", 1)
        if len(first) == 2 and ("." in first[0] or ":" in first[0] or first[0] == "localhost"):
            self.registry = first[0]
            ref = first[1]
        last = ref.rsplit("/", 1)[-1]
        if ":" in last:
            ref, self.tag = ref.rsplit(":", 1)
            self.tag_given = True
        self.repository = ref
        if self.registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
            self.registry = "docker.io"
            if "/" not in self.repository:
                self.repository = "library/" + self.repository

    @property
    def api_host(self) -> str:
        return "registry-1.docker.io" if self.registry == "docker.io" else self.registry

    @property
    def key(self) -> str:
        return "%s/%s:%s" % (self.registry, self.repository, self.tag)


def parse_semver(tag: str) -> Optional[Tuple[int, int, int, str]]:
    """'v1.2.3-alpine' -> (1, 2, 3, 'alpine'); None if the tag is not version-like."""
    m = re.match(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?$", tag)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), m.group(4) or "")


def find_newer_tag(current: str, tags: List[str]) -> Optional[str]:
    """Highest tag newer than `current` with the same suffix and the same style.

    '1.25.1' only compares with 'X.Y.Z' tags, '16-alpine' only with 'N-alpine',
    'v2.10.4' only with 'vX.Y.Z'. Pre-releases with a different suffix are ignored.
    """
    cur = parse_semver(current)
    if not cur:
        return None
    best, best_core = None, cur[:3]
    dots, vpref = current.count("."), current[:1] in "vV"
    for t in tags:
        p = parse_semver(t)
        if not p or p[3] != cur[3] or t.count(".") != dots or (t[:1] in "vV") != vpref:
            continue
        if p[:3] > best_core:
            best, best_core = t, p[:3]
    return best


def pick_local_digest(repo_digests: List[str], ref: ImageRef) -> Optional[str]:
    """Pick the RepoDigest entry that belongs to the same registry/repository."""
    for entry in repo_digests or []:
        if "@" not in entry:
            continue
        name, digest = entry.split("@", 1)
        other = ImageRef(name)
        if other.registry == ref.registry and other.repository == ref.repository:
            return digest
    return None


# ---------------------------------------------------------------------------
# Local Docker socket (read-only GET requests)
# ---------------------------------------------------------------------------


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTP over a local UNIX domain socket."""

    def __init__(self, socket_path: str, timeout: float = 5) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class DockerClient:
    """Only GET requests: /containers/json and /images/<id>/json."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET, timeout: float = 5) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def get_json(self, path: str) -> Any:
        conn = UnixSocketHTTPConnection(self.socket_path, timeout=self.timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise DockerError("Docker API %s returned HTTP %d" % (path, resp.status))
            return json.loads(body.decode("utf-8"))
        except DockerError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise DockerError("cannot read Docker socket %s: %s" % (self.socket_path, exc))
        finally:
            conn.close()

    def list_containers(self) -> List[Dict[str, Any]]:
        data = self.get_json("/containers/json")
        if not isinstance(data, list):
            raise DockerError("unexpected /containers/json response")
        return data

    def inspect_image(self, image_id: str) -> Optional[Dict[str, Any]]:
        try:
            data = self.get_json("/images/%s/json" % urllib.parse.quote(image_id, safe=":"))
        except DockerError:
            return None
        return data if isinstance(data, dict) else None


def running_containers(docker: DockerClient) -> List[Dict[str, Any]]:
    """[{name, image, local_digest}] for every running container."""
    out = []
    for c in docker.list_containers():
        names = c.get("Names") or ["/unknown"]
        name = (names[0] or "/unknown").lstrip("/")
        image = c.get("Image", "") or ""
        local_digest = None
        image_id = c.get("ImageID", "")
        if image_id and image and not image.startswith("sha256:"):
            info = docker.inspect_image(image_id)
            if info:
                local_digest = pick_local_digest(info.get("RepoDigests") or [], ImageRef(image))
        out.append({"name": name, "image": image, "local_digest": local_digest})
    out.sort(key=lambda x: x["name"])
    return out


# ---------------------------------------------------------------------------
# Registry (Docker Registry v2 / OCI distribution API, anonymous only)
# ---------------------------------------------------------------------------


def parse_bearer_challenge(header: str) -> Dict[str, str]:
    """'Bearer realm="...",service="...",scope="..."' -> dict."""
    out: Dict[str, str] = {}
    if not header or not header.lower().startswith("bearer "):
        return out
    for m in re.finditer(r'([A-Za-z_]+)="([^"]*)"', header[7:]):
        out[m.group(1).lower()] = m.group(2)
    return out


class RegistryClient:
    """HEAD manifests and list tags with an anonymous bearer token when asked for one."""

    def __init__(self, timeout: float = 10, urlopen: Optional[Callable[..., Any]] = None) -> None:
        self.timeout = timeout
        self.urlopen = urlopen or urllib.request.urlopen
        self.tokens: Dict[str, str] = {}
        self.ctx = ssl.create_default_context()

    def _token(self, challenge: Dict[str, str], repository: str) -> Optional[str]:
        realm = challenge.get("realm", "")
        if not realm.startswith("https://"):
            return None
        params = {"scope": challenge.get("scope") or "repository:%s:pull" % repository}
        if challenge.get("service"):
            params["service"] = challenge["service"]
        url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(params)
        if url in self.tokens:
            return self.tokens[url]
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None
        token = data.get("token") or data.get("access_token") if isinstance(data, dict) else None
        if token:
            self.tokens[url] = token
        return token

    def _request(self, url: str, method: str, accept: str, repository: str) -> Tuple[Any, bytes]:
        """Returns (headers, body). Raises RuntimeError with a short reason."""
        headers = {"User-Agent": USER_AGENT}
        if accept:
            headers["Accept"] = accept
        for attempt in (1, 2):
            req = urllib.request.Request(url, headers=headers, method=method)
            try:
                with self.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                    return resp.headers, (resp.read() if method == "GET" else b"")
            except urllib.error.HTTPError as err:
                if err.code == 401 and attempt == 1:
                    token = self._token(
                        parse_bearer_challenge(err.headers.get("Www-Authenticate", "")), repository
                    )
                    if token:
                        headers["Authorization"] = "Bearer " + token
                        continue
                    raise RuntimeError("registry asked for credentials: private or missing image (lite checks public images only)")
                if err.code == 401:
                    raise RuntimeError("registry asked for credentials: private or missing image (lite checks public images only)")
                if err.code == 404:
                    raise RuntimeError("tag or repository not found on the registry")
                if err.code == 429:
                    raise RuntimeError("registry rate limit hit (HTTP 429), try later")
                raise RuntimeError("registry HTTP %d" % err.code)
            except (urllib.error.URLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise RuntimeError("registry unreachable: %s" % reason)
        raise RuntimeError("registry request failed")

    def remote_digest(self, ref: ImageRef) -> str:
        url = "https://%s/v2/%s/manifests/%s" % (ref.api_host, ref.repository, ref.tag)
        hdrs, _ = self._request(url, "HEAD", MANIFEST_ACCEPT, ref.repository)
        digest = hdrs.get("Docker-Content-Digest") if hdrs is not None else None
        if not digest:
            raise RuntimeError("registry did not return a digest")
        return digest

    def list_tags(self, ref: ImageRef, max_pages: int = 3) -> List[str]:
        host = ref.api_host
        url: Optional[str] = "https://%s/v2/%s/tags/list?n=1000" % (host, ref.repository)
        tags: List[str] = []
        for _ in range(max_pages):
            if not url:
                break
            hdrs, body = self._request(url, "GET", "", ref.repository)
            try:
                tags.extend(json.loads((body or b"{}").decode("utf-8")).get("tags") or [])
            except (ValueError, AttributeError):
                raise RuntimeError("invalid tag list response")
            url = None
            m = re.search(r'<([^>]+)>;\s*rel="next"', (hdrs.get("Link", "") if hdrs is not None else "") or "")
            if m:
                nxt = m.group(1)
                if nxt.startswith("/"):
                    nxt = "https://%s%s" % (host, nxt)
                if nxt.startswith("https://%s/" % host):
                    url = nxt
        return tags


# ---------------------------------------------------------------------------
# Check + report
# ---------------------------------------------------------------------------

UPDATE, OK, SKIP, ERROR = "UPDATE", "OK", "SKIP", "ERROR"


def check_containers(
    containers: List[Dict[str, Any]], registry: Any, tag_check: bool = True
) -> List[Dict[str, Any]]:
    """Return one result per container: {name, image, status, detail}.

    Each distinct image is asked from the registry once.
    """
    cache: Dict[str, Tuple[str, str]] = {}
    results = []
    for c in containers:
        image = c.get("image") or ""
        if not image or image.startswith("sha256:"):
            results.append(dict(c, status=SKIP, detail="started from an image ID, no tag to check"))
            continue
        ref = ImageRef(image)
        if ref.digest and not ref.tag_given:
            results.append(dict(c, status=SKIP, detail="pinned by digest, no tag to check"))
            continue
        local = c.get("local_digest") or (ref.digest or None)
        if not local:
            results.append(dict(c, status=SKIP, detail="no registry digest locally (built or loaded locally?)"))
            continue
        ck = ref.key + "|" + local
        if ck not in cache:
            cache[ck] = _check_one(ref, local, registry, tag_check)
        status, detail = cache[ck]
        results.append(dict(c, status=status, detail=detail))
    return results


def _check_one(ref: ImageRef, local: str, registry: Any, tag_check: bool) -> Tuple[str, str]:
    try:
        remote = registry.remote_digest(ref)
    except RuntimeError as exc:
        return ERROR, str(exc)
    reasons = []
    if remote != local:
        reasons.append("new digest for :%s (%s -> %s)" % (ref.tag, short(local), short(remote)))
    if tag_check and parse_semver(ref.tag):
        try:
            newer = find_newer_tag(ref.tag, registry.list_tags(ref))
        except RuntimeError:
            newer = None  # the digest result still counts; tag list is best-effort
        if newer:
            reasons.append("newer tag %s" % newer)
    if reasons:
        return UPDATE, "; ".join(reasons)
    return OK, "digest matches :%s" % ref.tag


def short(digest: str) -> str:
    return digest.split(":", 1)[-1][:12] if digest else "?"


def render(results: List[Dict[str, Any]], header: str) -> str:
    lines = [header, ""]
    if not results:
        lines.append("No running containers found.")
        return "\n".join(lines)
    wn = max(9, max(len(r["name"]) for r in results))
    wi = max(5, max(len(r["image"]) for r in results))
    for r in results:
        lines.append("  %-6s  %-*s  %-*s  %s" % (r["status"], wn, r["name"], wi, r["image"], r["detail"]))
    counts = {s: sum(1 for r in results if r["status"] == s) for s in (UPDATE, OK, SKIP, ERROR)}
    lines.append("")
    lines.append(
        "%d container(s): %d update(s) available, %d up to date, %d skipped, %d could not be checked."
        % (len(results), counts[UPDATE], counts[OK], counts[SKIP], counts[ERROR])
    )
    if counts[UPDATE]:
        lines.append("Nothing was pulled or restarted. Review the changelog, then update when you choose.")
    return "\n".join(lines)


def exit_code(results: List[Dict[str, Any]]) -> int:
    if any(r["status"] == UPDATE for r in results):
        return 1
    checkable = [r for r in results if r["status"] != SKIP]
    if checkable and all(r["status"] == ERROR for r in checkable):
        return 2
    return 0


# ---------------------------------------------------------------------------
# Offline demo
# ---------------------------------------------------------------------------

DEMO_CONTAINERS = [
    {"name": "gitea", "image": "gitea/gitea:1.21.0", "local_digest": "sha256:" + "a1" * 32},
    {"name": "redis-cache", "image": "redis:7.2-alpine", "local_digest": "sha256:" + "b2" * 32},
    {"name": "traefik", "image": "traefik:v2.10.4", "local_digest": "sha256:" + "c3" * 32},
    {"name": "my-app", "image": "my-app:dev", "local_digest": None},
]


class DemoRegistry:
    """Canned registry answers for --demo (no network)."""

    DIGESTS = {
        "docker.io/gitea/gitea:1.21.0": "sha256:" + "a1" * 32,
        "docker.io/library/redis:7.2-alpine": "sha256:" + "d4" * 32,
        "docker.io/library/traefik:v2.10.4": "sha256:" + "c3" * 32,
    }
    TAGS = {
        "docker.io/gitea/gitea": ["1.20.5", "1.21.0", "1.21.3", "latest", "1.22.0-rc1"],
        "docker.io/library/redis": ["7.2-alpine", "7.4-alpine", "7.2"],
        "docker.io/library/traefik": ["v2.10.3", "v2.10.4", "v3.0", "latest"],
    }

    def remote_digest(self, ref: ImageRef) -> str:
        return self.DIGESTS[ref.key]

    def list_tags(self, ref: ImageRef) -> List[str]:
        return self.TAGS.get("%s/%s" % (ref.registry, ref.repository), [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_socket() -> str:
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        return host[len("unix://"):]
    return DEFAULT_SOCKET


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imagenudge-lite",
        description=(
            "List running containers on the local Docker host and report which ones have a newer "
            "image on the registry (new digest for the same tag, or a newer version tag). "
            "Read-only: never pulls, never restarts. Exit 0 = up to date, 1 = updates available, 2 = error."
        ),
        epilog="Full version with compose files, alerts and JSON: " + PRO_URL,
    )
    p.add_argument(
        "--socket",
        default=None,
        help="Docker socket path (default: DOCKER_HOST if it is unix://..., else %s)" % DEFAULT_SOCKET,
    )
    p.add_argument("--no-tags", action="store_true", help="only compare digests, skip the newer-tag lookup")
    p.add_argument("--demo", action="store_true", help="offline demo with sample containers (no Docker, no network)")
    p.add_argument("--timeout", type=float, default=10, help="network timeout in seconds (default: 10)")
    p.add_argument("--version", action="version", version="imagenudge-lite " + __version__)
    return p


def main(argv: Optional[List[str]] = None, registry: Any = None, docker: Any = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if args.timeout <= 0:
        print("error: --timeout must be > 0", file=sys.stderr)
        return 2

    if args.demo:
        results = check_containers(DEMO_CONTAINERS, DemoRegistry(), tag_check=not args.no_tags)
        print(render(results, "imagenudge-lite %s: demo (sample data, no Docker, no network)" % __version__))
        return exit_code(results)

    sock = args.socket or default_socket()
    docker = docker or DockerClient(sock, timeout=min(args.timeout, 10))
    try:
        containers = running_containers(docker)
    except DockerError as exc:
        print("error: %s" % exc, file=sys.stderr)
        print("hint: is Docker running, and can this user read the socket? Try --demo to see the output.", file=sys.stderr)
        return 2
    registry = registry or RegistryClient(timeout=args.timeout)
    results = check_containers(containers, registry, tag_check=not args.no_tags)
    print(render(results, "imagenudge-lite %s: %s" % (__version__, sock)))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
