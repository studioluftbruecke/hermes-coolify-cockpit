"""Behaviour over a real socket: which git host redirects a branch read follows.

A public repository that was renamed or moved answers its old address with a redirect on the same
host (GitHub does this for every rename). A read without a credential follows it there, so
"branch ahead" keeps working; a read that carries the git host token never follows one, and no
read follows a redirect to another host.
"""

import importlib

import pytest

REFS = (b"001e# service=git-upload-pack\n0000"
        b"003f" + b"a" * 40 + b" refs/heads/main\n0000")
UPLOAD_PACK = "/info/refs?service=git-upload-pack"


def _modules(plugin):
    return (importlib.import_module(plugin.__name__ + ".branches"),
            importlib.import_module(plugin.__name__ + ".coolify_client"))


def _github_via(server, client):
    """The real urllib transport, with https://github.com served by the local server."""
    def transport(method, url, headers, body, timeout):
        return client.urllib_transport(method, url.replace("https://github.com", server.url), headers, body, timeout)
    return transport


def _renamed(location_for):
    def route(method, path, headers):
        if path.startswith("/old/repo.git/"):
            return 301, {"Location": location_for(path[len("/old/repo.git"):])}, b""
        if path.startswith("/new/repo.git/"):
            return 200, {"Content-Type": "application/x-git-upload-pack-advertisement"}, REFS
        return 404, {}, b""
    return route


@pytest.mark.parametrize("location", [
    lambda rest: "https://github.com/new/repo.git" + rest,
    lambda rest: "/new/repo.git" + rest,
], ids=["absolute", "relative"])
def test_a_renamed_public_repository_is_still_read(plugin, local_http, location):
    branches, client = _modules(plugin)
    git = local_http["coolify"]
    git.route = _renamed(location)

    got = branches.read_public(_github_via(git, client), branches.parse_repo("https://github.com/old/repo"), "main")

    assert got == {"head": "a" * 40}
    assert [r[1] for r in git.requests] == ["/old/repo.git" + UPLOAD_PACK, "/new/repo.git" + UPLOAD_PACK]
    assert not any(r[2].get("Authorization") for r in git.requests)


def test_a_public_read_never_follows_a_redirect_to_another_host(plugin, local_http):
    branches, client = _modules(plugin)
    git, elsewhere = local_http["coolify"], local_http["elsewhere"]
    git.route = lambda m, p, h: (301, {"Location": f"http://localhost:{elsewhere.port}/new/repo.git{UPLOAD_PACK}"}, b"")

    with pytest.raises(branches.ReadError) as raised:
        branches._http(client.urllib_transport, git.url + "/old/repo.git" + UPLOAD_PACK, {})

    assert elsewhere.requests == []
    assert raised.value.kind == "failed" and "not followed" in str(raised.value)


def test_a_public_read_never_follows_https_down_to_http(plugin, local_http):
    branches, client = _modules(plugin)
    git = local_http["coolify"]
    git.route = lambda m, p, h: (301, {"Location": "http://github.com/new/repo.git" + UPLOAD_PACK}, b"")

    with pytest.raises(branches.ReadError):
        branches.read_public(_github_via(git, client), branches.parse_repo("https://github.com/old/repo"), "main")
    assert len(git.requests) == 1


def test_a_read_with_the_git_host_token_follows_no_redirect_even_on_its_own_host(plugin, local_http):
    branches, client = _modules(plugin)
    git = local_http["coolify"]
    git.route = _renamed(lambda rest: "/new/repo.git" + rest)
    secret = "ghp_" + "y" * 36

    with pytest.raises(branches.ReadError) as raised:
        branches._http(client.urllib_transport, git.url + "/old/repo.git" + UPLOAD_PACK,
                       {"Authorization": f"token {secret}"})

    assert len(git.requests) == 1 and raised.value.kind == "failed"
    assert secret not in str(raised.value)


def test_a_redirect_loop_ends_after_a_few_hops(plugin, local_http):
    branches, client = _modules(plugin)
    git = local_http["coolify"]
    git.route = lambda m, p, h: (302, {"Location": p}, b"")

    with pytest.raises(branches.ReadError) as raised:
        branches._http(client.urllib_transport, git.url + "/loop.git" + UPLOAD_PACK, {})

    assert raised.value.kind == "failed"
    assert 1 < len(git.requests) <= 5


def test_a_public_repository_read_without_a_redirect_is_unchanged(plugin, local_http):
    branches, client = _modules(plugin)
    git = local_http["coolify"]
    git.route = _renamed(lambda rest: "/unused")

    got = branches.read_public(_github_via(git, client), branches.parse_repo("https://github.com/new/repo"), "main")

    assert got == {"head": "a" * 40} and len(git.requests) == 1
