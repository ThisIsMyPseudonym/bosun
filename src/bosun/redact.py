"""One place that strips credentials from text before Bosun stores it or hands it to anyone.

Used for trace spans (command lines, stderr), the structured log, diagnostic bundles and, later, anything sent
to an LLM. It errs toward over-redacting: a troubleshooting bundle missing one token is fine, a bundle
leaking one is not.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable
from typing import Any

MASK = "[redacted]"

# Known token shapes (provider prefixes are distinctive, so these rarely hit ordinary text).
_TOKENS = [
    r"gh[pousr]_[A-Za-z0-9]{20,}",  # GitHub
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"glpat-[A-Za-z0-9_-]{20,}",  # GitLab
    r"sk-ant-[A-Za-z0-9_-]{20,}",  # Anthropic
    r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}",  # OpenAI
    r"xox[abposr]-[A-Za-z0-9-]{10,}",  # Slack
    r"(?:AKIA|ASIA)[A-Z0-9]{16}",  # AWS access key id
    r"AIza[0-9A-Za-z_-]{35}",  # Google API key
    r"npm_[A-Za-z0-9]{36}",
    r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",  # Stripe
    r"SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}",  # SendGrid
    r"pypi-[A-Za-z0-9_-]{50,}",
    r"hf_[A-Za-z0-9]{30,}",  # Hugging Face
    r"dop_v1_[a-f0-9]{64}",  # DigitalOcean
    r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",  # Telegram bot
    r"secret_[A-Za-z0-9]{43}", r"ntn_[A-Za-z0-9]{40,}",  # Notion
    r"lin_api_[A-Za-z0-9]{40}",  # Linear
    r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWT
    r"bsn_(?:at|rk|tt|tr)_[A-Za-z0-9_-]{20,}",  # Bosun per-turn assistant/triage MCP tokens and reply keys
]
_TOKEN_RE = re.compile("|".join(f"(?:{p})" for p in _TOKENS))
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
# user:password@, :password@ (redis), and a bare token as the user (https://<PAT>@dev.azure.com)
# (the lookbehinds here and in _KV_RE make a match start only at the start of a word: without them a long run of
# letters is rescanned from every position, and one 500 KB line of output takes minutes)
_URL_CRED_RE = re.compile(r"(?<![A-Za-z0-9+.-])(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/\s:@]*)(?::(?P<pw>[^/\s@]+))?@", re.I)
_CURL_USER_RE = re.compile(r"(?P<k>(?:^|\s)(?:-u[ =]*|--user[ =]+)[^\s:]+:)\S+")
_AUTH_RE = re.compile(r"(?P<k>\b(?:authorization|proxy-authorization)[\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic|token)\s+)?[^\s,;\"']+",
                      re.I)
_BEARER_RE = re.compile(r"(?P<k>\bbearer\s+)[A-Za-z0-9._~+/=-]{12,}", re.I)
_SECRET_NAME = (r"[A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|passphrase|pwd|access[_-]?key|account[_-]?key|"
                r"private[_-]?key|credential|cookie|authorization|(?<![A-Za-z])auth(?![a-z])|(?<![A-Za-z])pat(?![A-Za-z]))[A-Za-z0-9_.-]*")
# NAME=value, NAME: value, --name value / --name=value, "name": "value"
_KV_RE = re.compile(
    rf"(?P<k>(?<![A-Za-z0-9_.-]){_SECRET_NAME}\b[\"']?\s*(?:=|:|\s)\s*)"
    rf"(?P<v>\"[^\"\n]+\"|'[^'\n]+'|[^\s\"',;&]{{6,}})", re.I)  # a quoted value is masked whole, spaces included

_ENV_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
_env_lock = threading.Lock()
_env_values: tuple[str, ...] | None = None


def _environment_secrets() -> tuple[str, ...]:
    """Values of this process's own secret-looking env vars (NVD_API_KEY, GITHUB_TOKEN...): mask them anywhere."""
    global _env_values
    vals = _env_values
    if vals is None:
        with _env_lock:
            if _env_values is None:
                found = {v for k, v in os.environ.items() if _ENV_SECRET_NAME.search(k) and len(v) >= 8}
                _env_values = tuple(sorted(found, key=len, reverse=True))
            vals = _env_values
    return vals


def add_secrets(values: Iterable[str]) -> None:
    """Values Bosun learns are secret at runtime (e.g. the askpass channel): mask them from now on."""
    global _env_values
    with _env_lock:
        if _env_values is None:
            found = {v for k, v in os.environ.items() if _ENV_SECRET_NAME.search(k) and len(v) >= 8}
        else:
            found = set(_env_values)
        added = False
        for v in values:
            if v and len(v) >= 6 and v not in found:
                found.add(v)
                added = True
        if added or _env_values is None:
            _env_values = tuple(sorted(found, key=len, reverse=True))


def _url_cred(m: re.Match) -> str:
    if m["pw"] is not None:
        return f"{m['scheme']}{m['user']}:{MASK}@"
    # no password: "git@" and short account names are not secrets, a long opaque user is a token
    return m[0] if len(m["user"]) < 16 else f"{m['scheme']}{MASK}@"


def strip_userinfo(url: str) -> str:
    """https://user:token@host/x -> https://host/x: for URLs Bosun keeps as identifiers."""
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@\s]*@", r"\1", url, flags=re.I)


def text(s: str | None) -> str:
    if not s:
        return s or ""
    for v in _environment_secrets():
        if v in s:
            s = s.replace(v, MASK)
    s = _PEM_RE.sub(MASK, s)
    s = _URL_CRED_RE.sub(_url_cred, s)
    s = _CURL_USER_RE.sub(lambda m: f"{m['k']}{MASK}", s)
    s = _AUTH_RE.sub(lambda m: f"{m['k']}{MASK}", s)
    s = _BEARER_RE.sub(lambda m: f"{m['k']}{MASK}", s)
    s = _TOKEN_RE.sub(MASK, s)
    s = _KV_RE.sub(lambda m: m[0] if m["v"].strip("\"'") == MASK else f"{m['k']}{MASK}", s)
    return s


def argv(args: Iterable[str], max_arg: int = 300) -> list[str]:
    """A command line safe to store: secrets masked, the value after a secret-looking flag masked, long args cut
    (a system prompt or JSON schema passed inline is noise in a trace)."""
    out: list[str] = []
    mask_next = False
    for a in args:
        a = str(a)
        if mask_next:
            out.append(MASK)
            mask_next = False
            continue
        if a in ("-u", "--user"):  # curl-style user:password
            out.append(a)
            mask_next = True
            continue
        if a.startswith("-u") and len(a) > 2 and ":" in a[2:]:
            user, _ = a[2:].split(":", 1)
            out.append(f"-u{user}:{MASK}")
            continue
        if a.startswith("-") and "=" not in a and re.fullmatch(rf"--?{_SECRET_NAME}", a, re.I):
            out.append(a)
            mask_next = True
            continue
        a = text(a)
        out.append(a if len(a) <= max_arg else f"{a[:max_arg]}… ({len(a)} chars)")
    return out


def value(v: Any) -> Any:
    """Recursively redact strings inside JSON-like data; keys that name a secret get their value masked."""
    if isinstance(v, str):
        return text(v)
    if isinstance(v, dict):
        return {k: (MASK if isinstance(k, str) and re.fullmatch(_SECRET_NAME, k, re.I) and isinstance(x, str) and x
                    else value(x)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [value(x) for x in v]
    return v
