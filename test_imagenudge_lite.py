"""Offline tests for imagenudge-lite (fake registry, fake Docker socket, no network, no Docker)."""
import email.message
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

import imagenudge_lite as inl

D_OLD = "sha256:" + "1" * 64
D_NEW = "sha256:" + "2" * 64


def run(*argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = inl.main(list(argv), **kw)
    return code, out.getvalue(), err.getvalue()


class FakeRegistry:
    def __init__(self, digests=None, tags=None, fail=None):
        self.digests, self.tags, self.fail = digests or {}, tags or {}, fail or {}
        self.calls = []

    def remote_digest(self, ref):
        self.calls.append(ref.key)
        if ref.key in self.fail:
            raise RuntimeError(self.fail[ref.key])
        return self.digests[ref.key]

    def list_tags(self, ref):
        return self.tags.get(ref.registry + "/" + ref.repository, [])


class FakeDocker:
    def __init__(self, containers=None, images=None, error=None):
        self.containers, self.images, self.error = containers or [], images or {}, error

    def list_containers(self):
        if self.error:
            raise inl.DockerError(self.error)
        return self.containers

    def inspect_image(self, image_id):
        return self.images.get(image_id)


class FakeResp:
    def __init__(self, headers=None, body=b""):
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(url, code, headers=None):
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError(url, code, "err", h, None)


class TestImageRef(unittest.TestCase):
    def test_docker_hub_official(self):
        r = inl.ImageRef("postgres:15-alpine")
        self.assertEqual((r.registry, r.repository, r.tag, r.api_host),
                         ("docker.io", "library/postgres", "15-alpine", "registry-1.docker.io"))

    def test_default_latest_and_user_repo(self):
        r = inl.ImageRef("gitea/gitea")
        self.assertEqual((r.repository, r.tag, r.tag_given), ("gitea/gitea", "latest", False))

    def test_other_registry_tag_and_digest(self):
        r = inl.ImageRef("ghcr.io/org/repo:v2.1.0@sha256:abc")
        self.assertEqual((r.registry, r.repository, r.tag, r.digest), ("ghcr.io", "org/repo", "v2.1.0", "sha256:abc"))

    def test_registry_with_port(self):
        r = inl.ImageRef("localhost:5000/app")
        self.assertEqual((r.registry, r.repository, r.tag), ("localhost:5000", "app", "latest"))

    def test_docker_io_prefix(self):
        self.assertEqual(inl.ImageRef("docker.io/nginx:1").key, inl.ImageRef("nginx:1").key)


class TestTags(unittest.TestCase):
    TAGS = ["1.25.1", "1.25.3", "1.26.0", "1.27.0-alpine", "latest", "1.26", "2.0.0-rc1"]

    def test_parse_semver(self):
        self.assertEqual(inl.parse_semver("v1.2.3-alpine"), (1, 2, 3, "alpine"))
        self.assertEqual(inl.parse_semver("16"), (16, 0, 0, ""))
        self.assertIsNone(inl.parse_semver("latest"))

    def test_newer_tag_same_style_and_suffix(self):
        self.assertEqual(inl.find_newer_tag("1.25.1", self.TAGS), "1.26.0")
        self.assertEqual(inl.find_newer_tag("1.25.1-alpine", self.TAGS), "1.27.0-alpine")
        self.assertIsNone(inl.find_newer_tag("1.26.0", self.TAGS))
        self.assertIsNone(inl.find_newer_tag("latest", self.TAGS))
        self.assertEqual(inl.find_newer_tag("16-alpine", ["15-alpine", "17-alpine", "17.1-alpine"]), "17-alpine")
        self.assertEqual(inl.find_newer_tag("v2.10.4", ["2.11.0", "v2.11.0", "v3.0"]), "v2.11.0")

    def test_pick_local_digest(self):
        ref = inl.ImageRef("nginx:1")
        self.assertIsNone(inl.pick_local_digest(["other/app@sha256:aaa"], ref))
        self.assertEqual(inl.pick_local_digest(["other/app@sha256:aaa", "nginx@sha256:bbb"], ref), "sha256:bbb")
        self.assertEqual(inl.pick_local_digest(["ghcr.io/o/r@sha256:c"], inl.ImageRef("ghcr.io/o/r:1")), "sha256:c")


class TestCheck(unittest.TestCase):
    def test_up_to_date_update_and_skip(self):
        reg = FakeRegistry(
            digests={"docker.io/library/nginx:1.25.1": D_OLD, "docker.io/library/redis:7": D_NEW},
            tags={"docker.io/library/nginx": ["1.25.1", "1.26.0"]},
        )
        cs = [
            {"name": "web", "image": "nginx:1.25.1", "local_digest": D_OLD},
            {"name": "cache", "image": "redis:7", "local_digest": D_OLD},
            {"name": "app", "image": "my-app:dev", "local_digest": None},
            {"name": "raw", "image": "sha256:abc", "local_digest": None},
            {"name": "pin", "image": "redis@sha256:abc", "local_digest": None},
        ]
        res = {r["name"]: r for r in inl.check_containers(cs, reg)}
        self.assertEqual(res["web"]["status"], inl.UPDATE)
        self.assertIn("newer tag 1.26.0", res["web"]["detail"])
        self.assertNotIn("digest", res["web"]["detail"].split(";")[0][:10])
        self.assertEqual(res["cache"]["status"], inl.UPDATE)
        self.assertIn("new digest", res["cache"]["detail"])
        for n in ("app", "raw", "pin"):
            self.assertEqual(res[n]["status"], inl.SKIP, n)
        self.assertEqual(inl.exit_code(list(res.values())), 1)

    def test_no_tags_flag_only_digests(self):
        reg = FakeRegistry(digests={"docker.io/library/nginx:1.25.1": D_OLD},
                           tags={"docker.io/library/nginx": ["1.26.0"]})
        res = inl.check_containers([{"name": "w", "image": "nginx:1.25.1", "local_digest": D_OLD}], reg, tag_check=False)
        self.assertEqual(res[0]["status"], inl.OK)
        self.assertEqual(inl.exit_code(res), 0)

    def test_registry_queried_once_per_image(self):
        reg = FakeRegistry(digests={"docker.io/library/nginx:1": D_OLD})
        cs = [{"name": "a%d" % i, "image": "nginx:1", "local_digest": D_OLD} for i in range(3)]
        inl.check_containers(cs, reg)
        self.assertEqual(len(reg.calls), 1)

    def test_errors_and_exit_codes(self):
        reg = FakeRegistry(digests={"docker.io/library/nginx:1": D_OLD},
                           fail={"ghcr.io/me/private:1": "registry asked for credentials"})
        err = {"name": "p", "image": "ghcr.io/me/private:1", "local_digest": D_OLD}
        ok = {"name": "w", "image": "nginx:1", "local_digest": D_OLD}
        self.assertEqual(inl.exit_code(inl.check_containers([err], reg)), 2)
        self.assertEqual(inl.exit_code(inl.check_containers([err, ok], reg)), 0)
        self.assertEqual(inl.exit_code([]), 0)

    def test_tag_list_failure_does_not_hide_digest_result(self):
        class R(FakeRegistry):
            def list_tags(self, ref):
                raise RuntimeError("boom")
        res = inl.check_containers([{"name": "w", "image": "nginx:1.2.3", "local_digest": D_OLD}],
                                   R(digests={"docker.io/library/nginx:1.2.3": D_NEW}))
        self.assertEqual(res[0]["status"], inl.UPDATE)


class TestCLI(unittest.TestCase):
    def docker(self):
        return FakeDocker(
            containers=[
                {"Names": ["/web"], "Image": "nginx:1.25.1", "ImageID": "sha256:img1"},
                {"Names": ["/db"], "Image": "postgres:16", "ImageID": "sha256:img2"},
            ],
            images={
                "sha256:img1": {"RepoDigests": ["nginx@" + D_OLD]},
                "sha256:img2": {"RepoDigests": ["postgres@" + D_OLD]},
            },
        )

    def test_all_up_to_date_exit_0(self):
        reg = FakeRegistry(digests={"docker.io/library/nginx:1.25.1": D_OLD, "docker.io/library/postgres:16": D_OLD})
        code, out, _ = run("--socket", "/x.sock", docker=self.docker(), registry=reg)
        self.assertEqual(code, 0)
        self.assertIn("2 container(s): 0 update(s) available, 2 up to date", out)
        self.assertLess(out.index(" db "), out.index(" web "))

    def test_update_exit_1(self):
        reg = FakeRegistry(digests={"docker.io/library/nginx:1.25.1": D_NEW, "docker.io/library/postgres:16": D_OLD},
                           tags={"docker.io/library/postgres": ["16", "17", "17.1"]})
        code, out, _ = run(docker=self.docker(), registry=reg)
        self.assertEqual(code, 1)
        self.assertIn("UPDATE  web", out)
        self.assertIn("newer tag 17", out)
        self.assertIn("Nothing was pulled or restarted", out)

    def test_docker_unreachable_exit_2(self):
        code, _, err = run(docker=FakeDocker(error="cannot read Docker socket"), registry=FakeRegistry())
        self.assertEqual(code, 2)
        self.assertIn("cannot read Docker socket", err)

    def test_no_containers(self):
        code, out, _ = run(docker=FakeDocker(), registry=FakeRegistry())
        self.assertEqual(code, 0)
        self.assertIn("No running containers found.", out)

    def test_demo_offline(self):
        code, out, _ = run("--demo")
        self.assertEqual(code, 1)
        self.assertIn("demo", out)
        self.assertIn("SKIP    my-app", out)
        self.assertIn("OK      traefik", out)

    def test_bad_args_exit_2_and_version(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(run("--bogus")[0], 2)
            self.assertEqual(run("--timeout", "0")[0], 2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run("--version")[0], 0)

    def test_docker_host_env(self):
        old = os.environ.get("DOCKER_HOST")
        try:
            os.environ["DOCKER_HOST"] = "unix:///run/user/1000/docker.sock"
            self.assertEqual(inl.default_socket(), "/run/user/1000/docker.sock")
            os.environ["DOCKER_HOST"] = "tcp://10.0.0.5:2375"
            self.assertEqual(inl.default_socket(), inl.DEFAULT_SOCKET)
        finally:
            if old is None:
                os.environ.pop("DOCKER_HOST", None)
            else:
                os.environ["DOCKER_HOST"] = old


class TestRegistryClient(unittest.TestCase):
    def test_anonymous_token_flow_and_digest(self):
        seen = []

        def urlopen(req, timeout=None, context=None):
            url = req.full_url
            seen.append((req.get_method(), url, req.headers.get("Authorization")))
            if url.startswith("https://auth.example/token"):
                self.assertIn("scope=repository%3Alibrary%2Fnginx%3Apull", url)
                return FakeResp(body=json.dumps({"token": "T0K"}).encode())
            if not req.headers.get("Authorization"):
                raise http_error(url, 401, {"Www-Authenticate":
                    'Bearer realm="https://auth.example/token",service="registry.example",scope="repository:library/nginx:pull"'})
            return FakeResp({"Docker-Content-Digest": D_NEW})

        rc = inl.RegistryClient(urlopen=urlopen)
        self.assertEqual(rc.remote_digest(inl.ImageRef("nginx:1")), D_NEW)
        self.assertEqual(seen[0][0], "HEAD")
        self.assertEqual(seen[0][1], "https://registry-1.docker.io/v2/library/nginx/manifests/1")
        self.assertEqual(seen[-1][2], "Bearer T0K")
        # token is cached
        rc.remote_digest(inl.ImageRef("nginx:1"))
        self.assertEqual(sum(1 for s in seen if "auth.example" in s[1]), 1)

    def test_tag_list_pagination(self):
        pages = {
            "https://ghcr.io/v2/o/r/tags/list?n=1000": (["1.0.0", "1.1.0"], '</v2/o/r/tags/list?n=1000&last=1.1.0>; rel="next"'),
            "https://ghcr.io/v2/o/r/tags/list?n=1000&last=1.1.0": (["1.2.0"], ""),
        }

        def urlopen(req, timeout=None, context=None):
            tags, link = pages[req.full_url]
            return FakeResp({"Link": link} if link else {}, json.dumps({"tags": tags}).encode())

        rc = inl.RegistryClient(urlopen=urlopen)
        self.assertEqual(rc.list_tags(inl.ImageRef("ghcr.io/o/r:1.0.0")), ["1.0.0", "1.1.0", "1.2.0"])

    def test_error_messages(self):
        def make(code, hdr=None):
            def urlopen(req, timeout=None, context=None):
                raise http_error(req.full_url, code, hdr)
            return urlopen

        cases = [(404, None, "not found"), (429, None, "rate limit"), (401, None, "private or missing"),
                 (500, None, "HTTP 500")]
        for code, hdr, msg in cases:
            with self.assertRaises(RuntimeError) as cm:
                inl.RegistryClient(urlopen=make(code, hdr)).remote_digest(inl.ImageRef("nginx:1"))
            self.assertIn(msg, str(cm.exception))

        def offline(req, timeout=None, context=None):
            raise urllib.error.URLError("no network")
        with self.assertRaises(RuntimeError) as cm:
            inl.RegistryClient(urlopen=offline).remote_digest(inl.ImageRef("nginx:1"))
        self.assertIn("unreachable", str(cm.exception))

    def test_challenge_parse(self):
        c = inl.parse_bearer_challenge('Bearer realm="https://a/t",service="s",scope="repository:x:pull"')
        self.assertEqual(c, {"realm": "https://a/t", "service": "s", "scope": "repository:x:pull"})
        self.assertEqual(inl.parse_bearer_challenge("Basic realm=x"), {})


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs unix sockets")
class TestDockerSocket(unittest.TestCase):
    """A tiny fake Docker API on a temp unix socket (no Docker needed)."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="inl")
        self.path = os.path.join(self.d, "d.sock")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(5)
        self.requests = []
        self.routes = {
            "/containers/json": [{"Names": ["/web"], "Image": "nginx:1", "ImageID": "sha256:i1"}],
            "/images/sha256:i1/json": {"RepoDigests": ["nginx@" + D_OLD]},
        }
        self.t = threading.Thread(target=self.serve, daemon=True)
        self.t.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                line = data.split(b"\r\n", 1)[0].decode()
                method, path = line.split(" ")[:2]
                self.requests.append((method, path))
                if path in self.routes:
                    body, status = json.dumps(self.routes[path]).encode(), "200 OK"
                else:
                    body, status = b'{"message":"no such image"}', "404 Not Found"
                conn.sendall(("HTTP/1.1 %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                              "Connection: close\r\n\r\n" % (status, len(body))).encode() + body)

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_reads_containers_read_only(self):
        cs = inl.running_containers(inl.DockerClient(self.path))
        self.assertEqual(cs, [{"name": "web", "image": "nginx:1", "local_digest": D_OLD}])
        self.assertTrue(all(m == "GET" for m, _ in self.requests))

    def test_cli_against_socket(self):
        reg = FakeRegistry(digests={"docker.io/library/nginx:1": D_NEW})
        code, out, _ = run("--socket", self.path, registry=reg)
        self.assertEqual(code, 1)
        self.assertIn("new digest for :1", out)

    def test_missing_socket(self):
        with self.assertRaises(inl.DockerError):
            inl.DockerClient(os.path.join(self.d, "missing.sock")).list_containers()


if __name__ == "__main__":
    unittest.main(verbosity=1)
