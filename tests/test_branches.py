"""0.1.0 M7: the behind_branch reads (branches.py) and the signal they feed (fleet.py).

Git host answers are recorded shapes (smart-HTTP ref advertisement, Forgejo/GitHub branch and
compare JSON, Forgebrücke's forgejo_get) with synthetic values. No network.
"""

import importlib
import importlib.util
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOYED = "a" * 40
HEAD = "b" * 40


def _load():
    name = "coolify_cockpit_branches_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    pkg = sys.modules[name]
    return types.SimpleNamespace(pkg=pkg, br=importlib.import_module(name + ".branches"),
                                 fleet=importlib.import_module(name + ".fleet"),
                                 client=importlib.import_module(name + ".coolify_client"))


@pytest.fixture
def m():
    return _load()


class Git:
    """A transport for git hosts: answers by URL, records (url, Authorization)."""

    def __init__(self, m, routes):
        self.m, self.routes, self.requests = m, routes, []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append((url, headers.get("Authorization")))
        status, payload = self.routes.get(url, (404, b"not found"))
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        return self.m.client.HttpResponse(status, payload)


def refs(branch, sha):
    head = f"{sha} refs/heads/{branch}"
    return (b"001e# service=git-upload-pack\n0000"
            + f"{len(head) + 4 + 1:04x}{head}\0multi_ack side-band-64k\n".encode()
            + f"003f{'c' * 40} refs/heads/other\n0000".encode())


def app(uuid, repo, branch="main", env="staging", name=None):
    return {"uuid": uuid, "kind": "application", "name": name or uuid, "environment": env,
            "git_repository": repo, "git_branch": branch}


def ok_row(commit=DEPLOYED, state="ok"):
    return {"state": state, "last_good": {"uuid": "d1", "commit": commit, "finished_at": "2026-11-09T10:00:00Z"}}


# --- parsing and where a token may go -------------------------------------------------------------

@pytest.mark.parametrize("raw, host, path", [
    ("git@forgejo.example.test:acme/shop.git", "forgejo.example.test", "acme/shop"),
    ("git@forgejo.example.test:22222/acme/shop.git", "forgejo.example.test", "acme/shop"),
    ("ssh://git@Forgejo.Example.test:2222/acme/shop.git", "forgejo.example.test", "acme/shop"),
    ("https://github.com/coollabsio/coolify", "github.com", "coollabsio/coolify"),
    ("coollabsio/coolify", "github.com", "coollabsio/coolify"),
])
def test_parse_repo_reads_every_form_coolify_stores(m, raw, host, path):
    repo = m.br.parse_repo(raw)
    assert (repo.host, repo.path) == (host, path)
    assert repo.https == f"https://{host}/{path}.git"


@pytest.mark.parametrize("raw", ["", None, "not a repo", "https://host/only-one-part", "git@host:a/b/c.git"])
def test_parse_repo_refuses_what_it_cannot_place(m, raw):
    assert m.br.parse_repo(raw) is None


def test_the_git_token_goes_only_to_its_own_host(m):
    assert m.br.token_host("ghp_abc", None) == "github.com"
    assert m.br.token_host("ghp_abc", "") == "github.com"
    assert m.br.token_host("0123abcd", None) is None  # a Forgejo token without a host goes nowhere
    assert m.br.token_host("0123abcd", "https://git.example.test/") == "git.example.test"
    assert m.br.token_host(None, "git.example.test") is None
    src = m.br.Sources(git_token="0123abcd", git_host="git.example.test", transport=object())
    assert src.plan(m.br.Repo("git.example.test", "a/b")) == "git_token"
    assert src.plan(m.br.Repo("github.com", "a/b")) == "public"


def test_forgebruecke_apps_wait_for_a_process_with_ctx(m):
    route = m.br.Sources(forgebruecke_host="forgejo.example.test", transport=object())
    tick = m.br.Sources(forgebruecke_host="forgejo.example.test", dispatch=lambda *a: None, transport=object())
    on_forge = m.br.Repo("forgejo.example.test", "acme/shop")
    assert route.plan(on_forge) is None and tick.plan(on_forge) == "forgebruecke"
    # an alias of the host is not Forgebrücke's host (P-2)
    assert tick.plan(m.br.Repo("forgejo-sl.example.test", "acme/shop")) == "public"


# --- the three reads ----------------------------------------------------------------------------

def test_public_read_finds_the_branch_in_the_ref_advertisement(m):
    repo = m.br.Repo("github.com", "coollabsio/coolify")
    url = "https://github.com/coollabsio/coolify.git/info/refs?service=git-upload-pack"
    git = Git(m, {url: (200, refs("main", HEAD))})
    assert m.br.read_public(git, repo, "main") == {"head": HEAD}
    assert git.requests == [(url, None)]  # no credential, ever
    with pytest.raises(m.br.ReadError) as missing:
        m.br.read_public(git, repo, "nope")
    assert missing.value.kind == "closed"


def test_public_read_of_a_private_repo_is_closed(m):
    git = Git(m, {})
    git.routes["https://forgejo.example.test/acme/shop.git/info/refs?service=git-upload-pack"] = (401, b"")
    with pytest.raises(m.br.ReadError) as closed:
        m.br.read_public(git, m.br.Repo("forgejo.example.test", "acme/shop"), "main")
    assert closed.value.kind == "closed"


def test_token_read_counts_the_commits_on_forgejo_and_github(m):
    forgejo = m.br.Repo("git.example.test", "acme/shop")
    git = Git(m, {
        "https://git.example.test/api/v1/repos/acme/shop/branches/main": (200, {"commit": {"id": HEAD}}),
        f"https://git.example.test/api/v1/repos/acme/shop/compare/{DEPLOYED}...{HEAD}": (200, {"total_commits": 3}),
        "https://api.github.com/repos/acme/blog/branches/release%2F1": (200, {"commit": {"sha": HEAD}}),
        f"https://api.github.com/repos/acme/blog/compare/{DEPLOYED}...{HEAD}": (200, {"ahead_by": 1}),
    })
    assert m.br.read_with_token(git, "tok", forgejo, "main", DEPLOYED) == {"head": HEAD, "commits": 3}
    assert m.br.read_with_token(git, "tok", m.br.Repo("github.com", "acme/blog"), "release/1", DEPLOYED) == {
        "head": HEAD, "commits": 1}
    assert {auth for _, auth in git.requests} == {"token tok"}


def test_token_read_without_compare_still_gives_the_head(m):
    git = Git(m, {"https://git.example.test/api/v1/repos/acme/shop/branches/main": (200, {"commit": {"id": HEAD}})})
    assert m.br.read_with_token(git, "tok", m.br.Repo("git.example.test", "acme/shop"), "main", DEPLOYED) == {"head": HEAD}


def test_forgebruecke_read_uses_forgejo_get_kind_branch(m):
    calls = []

    def dispatch(tool, args):
        calls.append((tool, args))
        return json.dumps({"ok": True, "kind": "branch", "branch": {"repo": args["repo"], "sha": HEAD}})

    assert m.br.read_forgebruecke(dispatch, m.br.Repo("forgejo.example.test", "acme/shop"), "staging") == {"head": HEAD}
    assert calls == [("forgejo_get", {"kind": "branch", "repo": "acme/shop", "ref": "staging"})]
    with pytest.raises(m.br.ReadError):
        m.br.read_forgebruecke(lambda t, a: json.dumps({"ok": False, "error": "unknown_tool"}),
                               m.br.Repo("forgejo.example.test", "acme/shop"), "staging")


# --- one round: cache, cadence, stale answers ---------------------------------------------------

def test_refresh_reads_once_per_ttl_and_drops_answers_for_an_older_deploy(m, tmp_path):
    resources = [app("app-a", "coollabsio/coolify")]
    fleet = {"app-a": ok_row()}
    url = "https://github.com/coollabsio/coolify.git/info/refs?service=git-upload-pack"
    git = Git(m, {url: (200, refs("main", HEAD))})
    src = m.br.Sources(transport=git)
    path = tmp_path / m.br.BRANCH_FILE
    out = m.br.refresh(src, resources, fleet, path=path, key="k", now=1000.0)
    assert out == {"app-a": {"head": HEAD, "source": "public"}}
    m.br.refresh(src, resources, fleet, path=path, key="k", now=1000.0 + m.br.BRANCH_TTL_S - 1)
    assert len(git.requests) == 1  # cached
    # a new deploy (another commit) makes the stored answer stale at once, and it is read again
    fleet2 = {"app-a": ok_row(commit=HEAD)}
    assert m.br.refresh(None, resources, fleet2, path=path, key="k", now=1001.0) == {}
    assert m.br.refresh(src, resources, fleet2, path=path, key="k", now=1002.0) == {"app-a": {"head": HEAD, "source": "public"}}
    assert len(git.requests) == 2
    assert "token" not in path.read_text() and m.br.refresh(None, resources, fleet2, path=path, key="other") == {}


def test_a_closed_repo_is_asked_again_only_after_hours(m, tmp_path):
    resources = [app("app-p", "git@forgejo.example.test:acme/private.git")]
    git = Git(m, {"https://forgejo.example.test/acme/private.git/info/refs?service=git-upload-pack": (401, b"")})
    src = m.br.Sources(transport=git)
    path = tmp_path / m.br.BRANCH_FILE
    for now in (0.0, m.br.BRANCH_TTL_S + 1, m.br.CLOSED_TTL_S - 1):
        assert m.br.refresh(src, resources, {"app-p": ok_row()}, path=path, key="k", now=now) == {}
    assert len(git.requests) == 1
    m.br.refresh(src, resources, {"app-p": ok_row()}, path=path, key="k", now=m.br.CLOSED_TTL_S + 1)
    assert len(git.requests) == 2


def test_refresh_skips_deploying_apps_short_shas_and_failed_reads_keep_the_last_answer(m, tmp_path):
    resources = [app("app-d", "coollabsio/coolify"), app("app-s", "acme/x"), app("app-f", "acme/f")]
    fleet = {"app-d": ok_row(state="deploying"), "app-s": ok_row(commit="abc1234"), "app-f": ok_row()}
    git = Git(m, {"https://github.com/acme/f.git/info/refs?service=git-upload-pack": (200, refs("main", HEAD))})
    path = tmp_path / m.br.BRANCH_FILE
    assert set(m.br.refresh(m.br.Sources(transport=git), resources, fleet, path=path, key="k", now=0.0)) == {"app-f"}
    assert [u for u, _ in git.requests] == ["https://github.com/acme/f.git/info/refs?service=git-upload-pack"]
    git.routes.clear()
    git.routes["https://github.com/acme/f.git/info/refs?service=git-upload-pack"] = (502, b"")
    out = m.br.refresh(m.br.Sources(transport=git), resources, fleet, path=path, key="k", now=m.br.BRANCH_TTL_S + 5)
    assert out["app-f"]["head"] == HEAD  # a 502 is no verdict: the last answer stays
    asked = len(git.requests)
    m.br.refresh(m.br.Sources(transport=git), resources, fleet, path=path, key="k", now=m.br.BRANCH_TTL_S + 60)
    assert len(git.requests) == asked  # and the host is not asked again before the TTL


def test_a_host_that_never_answers_is_asked_once_per_ttl(m, tmp_path):
    resources = [app("app-x", "git@dead.example.test:acme/x.git")]
    git = Git(m, {"https://dead.example.test/acme/x.git/info/refs?service=git-upload-pack": (503, b"")})
    path = tmp_path / m.br.BRANCH_FILE
    for now in (0.0, 30.0, 300.0):
        assert m.br.refresh(m.br.Sources(transport=git), resources, {"app-x": ok_row()}, path=path, key="k", now=now) == {}
    assert len(git.requests) == 1


# --- the signal ---------------------------------------------------------------------------------

def test_behind_branch_signal_words(m):
    snap = {"resources": [app("a1", "x/y", "main", "production", "shop"), app("a2", "x/y", "staging", "staging", "shop-st"),
                          app("a3", "x/z", "main", "production", "blog")],
            "fleet": {"a1": ok_row(), "a2": ok_row(), "a3": ok_row()}}
    view = m.fleet.View(snap, branches={"a1": {"head": HEAD, "commits": 3}, "a2": {"head": HEAD},
                                        "a3": {"head": HEAD, "commits": 0}})
    items = [a.to_dict() for a in m.fleet.attention(view)]
    assert [(a["resource_uuid"], a["title"], a["fix"]) for a in items] == [
        ("a1", "shop (production): main is 3 commits ahead", "deploy"),
        ("a2", "shop-st (staging): staging is ahead", "deploy")]  # a3: the head is behind what runs
    assert [a["badge"] for a in items] == ["3 ahead", "ahead"]  # the app row's badge (A14: "N ahead")
    assert m.fleet.summary(m.fleet.attention(view)) == [
        {"signal": "behind_branch", "label": "branch ahead", "severity": "warn", "count": 2}]


def test_served_snapshot_reads_branch_heads_through_forgebruecke_in_a_process_with_ctx(m, tmp_path, monkeypatch):
    """The seam: _Setup -> branch_sources -> branches.refresh -> fleet signal."""
    snap = {"resources": [app("a1", "git@forgejo.example.test:acme/shop.git", "staging", "staging", "shop-st")],
            "fleet": {"a1": ok_row()}}

    class FakeSetup:
        data_dir = tmp_path
        git_transport = None
        get_secret = staticmethod(lambda name: None)
        get_setting = staticmethod(lambda key, default=None: default)
        forgebruecke_host = staticmethod(lambda: "forgejo.example.test")
        snap = types.SimpleNamespace(cache_key=lambda c: "k")
        connection = None

    calls = []
    monkeypatch.setitem(m.pkg._CTX_DISPATCH, "dispatch", lambda tool, args: calls.append(args) or json.dumps(
        {"ok": True, "branch": {"sha": HEAD}}))
    assert m.pkg._branch_heads_for(FakeSetup, snap) == {"a1": {"head": HEAD, "source": "forgebruecke"}}
    assert calls == [{"kind": "branch", "repo": "acme/shop", "ref": "staging"}]
    monkeypatch.setitem(m.pkg._CTX_DISPATCH, "dispatch", None)  # a page route: reads the stored answer only
    assert m.pkg._branch_heads_for(FakeSetup, snap) == {"a1": {"head": HEAD, "source": "forgebruecke"}}
    assert len(calls) == 1


def test_reads_run_in_the_callers_context(m, tmp_path):
    """Inside an agent turn the profile's secrets live in a ContextVar; a bare worker thread does not
    see it, and Forgebrücke then answered without its token (0 of 23 reads in a live turn)."""
    import contextvars
    scope = contextvars.ContextVar("secret_scope_under_test", default=None)

    def dispatch(tool, args):
        if scope.get() is None:
            return json.dumps({"ok": False, "error": "not_configured"})
        return json.dumps({"ok": True, "branch": {"sha": HEAD}})

    resources = [app(f"a{i}", f"git@forgejo.example.test:acme/r{i}.git") for i in range(6)]
    fleet = {f"a{i}": ok_row() for i in range(6)}
    src = m.br.Sources(forgebruecke_host="forgejo.example.test", dispatch=dispatch)
    token = scope.set({"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": "x"})
    try:
        out = m.br.refresh(src, resources, fleet, path=tmp_path / m.br.BRANCH_FILE, key="k", now=0.0)
    finally:
        scope.reset(token)
    assert len(out) == 6 and {v["source"] for v in out.values()} == {"forgebruecke"}
