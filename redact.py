"""Mask secrets in log text before any of it reaches the model.

Build logs echo build arguments, `.env` writes and clone URLs, so a pasted or fetched deployment
log can carry tokens and passwords. `redact(text)` replaces every match with `[REDACTED:<kind>]`
and keeps the line count and line order, so line numbers computed after redaction still point at
the original log.

The rules are one table (`RULES`). `HERMES_PREFIX_PATTERNS` is the subset that fits Hermes'
`ctx.register_redaction_patterns` (each starts with at least two literal characters, has no
top-level alternation and no nested unbounded repeat); the plugin registers them at load, so Hermes
also masks these formats in its own logs and tool output. Coolify's API token format
(`<id>|<40 chars>`) starts with digits, so Hermes' rules refuse it, and it is masked here only.

Redaction is a best effort over known formats, not a guarantee; the README discloses that log
text can reach the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Union

MASK = "[REDACTED:{kind}]"


def mask(kind: str) -> str:
    return MASK.format(kind=kind)


# Token formats with a literal prefix. Exported for Hermes' redaction registry as well.
HERMES_PREFIX_PATTERNS: tuple[tuple[str, str], ...] = (
    ("github-token", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("github-token", r"github_pat_[A-Za-z0-9_]{22,}"),
    ("gitlab-token", r"glpat-[A-Za-z0-9_-]{20,}"),
    ("npm-token", r"npm_[A-Za-z0-9]{36}"),
    ("slack-token", r"xox[abposr]-[A-Za-z0-9-]{10,}"),
    ("aws-key-id", r"AKIA[0-9A-Z]{16}"),
    ("aws-key-id", r"ASIA[0-9A-Z]{16}"),
    ("google-api-key", r"AIza[0-9A-Za-z_-]{35}"),
    ("stripe-key", r"sk_live_[0-9A-Za-z]{16,}"),
    ("stripe-key", r"rk_live_[0-9A-Za-z]{16,}"),
    ("stripe-key", r"sk_test_[0-9A-Za-z]{16,}"),
    ("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai-key", r"sk-proj-[A-Za-z0-9_-]{20,}"),
    ("openai-key", r"sk-[A-Za-z0-9]{32,}"),
    ("sendgrid-key", r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
    ("digitalocean-token", r"dop_v1_[a-f0-9]{64}"),
    ("jwt", r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)

# Names whose value is a secret in KEY=value, KEY: value and "key": "value" forms. A bare PWD is
# the shell's working directory (every `env` dump prints it), so only DB_PWD-style names count.
SECRET_NAME = re.compile(
    r"(SECRET|TOKEN|PASSW(?:OR)?D|PASSPHRASE|(?:^|_)PASS(?:$|_)|_PWD(?:$|_)|API_?KEY|"
    r"ACCESS_?KEY|PRIVATE_?KEY|(?:^|_)AUTH(?:$|_)|CREDENTIAL|(?:^|_)SALT(?:$|_)|SIGNING_?KEY|"
    r"ENCRYPTION_?KEY|(?:^|_)DSN(?:$|_)|DATABASE_URL|(?:DB|REDIS|MONGO(?:DB)?|POSTGRES|MYSQL|AMQP|"
    r"RABBITMQ)_?UR[LI]|CONNECTION_STRING|SERVICE_BASE64|_AUTHTOKEN|SESSION_?KEY|COOKIE)",
    re.IGNORECASE)

# KEY=value / KEY: value / "KEY": "value". The value must not start with `$` (a reference like
# ${VAR} or $VAR is no secret) and stops at whitespace or a quote.
_KEY_VALUE = re.compile(
    r"""(?P<key>(?<![\w$-])[A-Za-z_][A-Za-z0-9_.-]*)(?P<q1>["']?)(?P<sep>[ \t]*=[ \t]*|[ \t]*:[ \t]+|[ \t]*:(?=["']))"""
    r"""(?P<q2>["']?)(?P<value>[^\s"'$<>\[][^\s"']*)""")


def _key_value(match: re.Match) -> str:
    if not SECRET_NAME.search(match.group("key")):
        return match.group(0)
    return (match.group("key") + match.group("q1") + match.group("sep") + match.group("q2")
            + mask("value"))


Replacement = Union[str, Callable[[re.Match], str]]


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern
    replace: Replacement


def _rules() -> tuple[Rule, ...]:
    rules = [
        # Coolify writes every env value as one base64 blob into the build container's .env:
        # `echo '<base64>' | base64 -d | tee /artifacts/.../.env`.
        Rule("coolify-env-blob",
             re.compile(r"(echo[ \t]+['\"]?)[A-Za-z0-9+/]{40,}={0,2}(['\"]?[ \t]*\|[ \t]*base64[ \t]+-d)"),
             lambda m: m.group(1) + mask("coolify-env-blob") + m.group(2)),
        Rule("coolify-token", re.compile(r"(?<![\w|])\d{1,8}\|[A-Za-z0-9]{40,}\b"), mask("coolify-token")),
        Rule("bearer", re.compile(r"(?i)\b(bearer[ \t]+)[A-Za-z0-9._~+/=|-]{12,}"),
             lambda m: m.group(1) + mask("bearer")),
        Rule("authorization", re.compile(r"(?i)(authorization:[ \t]*(?:basic|token)[ \t]+)[A-Za-z0-9._~+/=|-]{8,}"),
             lambda m: m.group(1) + mask("authorization")),
        # scheme://user:password@host  ->  scheme://user:[REDACTED]@host
        Rule("url-password", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@'\"]+:)[^\s/@'\"]+@"),
             lambda m: m.group(1) + mask("url-password") + "@"),
        # https://<token>@github.com (token as the only userinfo)
        Rule("url-token", re.compile(r"(?i)\b(https?://)[A-Za-z0-9_.~-]{16,}@"),
             lambda m: m.group(1) + mask("url-token") + "@"),
        Rule("docker-login-password", re.compile(r"(?i)(docker[ \t]+login\b[^\n]*?(?:-p|--password)[ \t=]+)\S+"),
             lambda m: m.group(1) + mask("password")),
    ]
    rules += [Rule(kind, re.compile(r"(?<![A-Za-z0-9])" + pattern), mask(kind))
              for kind, pattern in HERMES_PREFIX_PATTERNS]
    rules.append(Rule("value", _KEY_VALUE, _key_value))
    return tuple(rules)


RULES: tuple[Rule, ...] = _rules()

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def redact_line(line: str) -> tuple[str, int]:
    """Mask one line; return it with the number of masks added."""
    before = line.count("[REDACTED:")
    for rule in RULES:
        line = rule.pattern.sub(rule.replace, line)
    return line, line.count("[REDACTED:") - before


def redact(text: str) -> tuple[str, int]:
    """Return (text with secrets masked, number of masks). Never adds or removes a line."""
    lines = text.split("\n")
    out: list[str] = []
    total = 0
    in_pem = False
    for line in lines:
        if in_pem or _PEM_BEGIN.search(line):
            begins = _PEM_BEGIN.search(line)
            ends = _PEM_END.search(line)
            if begins and ends:  # a one-line key
                out.append(line[:begins.start()] + mask("private-key") + line[ends.end():])
            elif begins:
                out.append(line[:begins.start()] + mask("private-key"))
                in_pem = True
            elif ends:
                out.append(mask("private-key") + line[ends.end():])
                in_pem = False
            else:
                out.append(mask("private-key"))
            total += 1
            continue
        masked, count = redact_line(line)
        out.append(masked)
        total += count
    return "\n".join(out), total


def hermes_patterns() -> list[str]:
    """The regexes to hand to `ctx.register_redaction_patterns` (token formats only)."""
    return [pattern for _, pattern in HERMES_PREFIX_PATTERNS]
