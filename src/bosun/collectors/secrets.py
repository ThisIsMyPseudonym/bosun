"""Credentials in your code projects: committed, left in git history, or one `git add` away.

- a `.env` file tracked by git (anyone with the repo has those keys), or one that was committed
  once and deleted (still in the history: if it was pushed, the keys are out)
- a `.env` file in the working tree that .gitignore doesn't cover
- private keys and key stores tracked by git
- recognisable tokens (AWS, GitHub, OpenAI, Anthropic, Stripe, Slack, Google, npm, Hugging Face...)
  in tracked files, found with `git grep`

Test code is full of fixtures shaped like credentials: findings under test paths rank lower, and a
private-key header with no key under it isn't reported at all.

Only where a secret is and what kind it is gets recorded: never its value.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from .. import proc
from ..config import Settings
from ..models import Item, Source, Summary
from .repos import find_repos

log = logging.getLogger(__name__)

# .env files meant to be shared: placeholders, not values
_ENV_TEMPLATE = re.compile(r"\.(example|sample|template|dist|defaults?|schema|tpl)$", re.I)
_ENV_NAME = re.compile(r"^\.env(\..+)?$")
_KEY_FILES = ("*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
_SECRET_LINE = re.compile(r"^\s*(export\s+)?\w*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|CREDENTIAL)\w*\s*=\s*['\"]?[^'\"\s#$]{6,}", re.I)

# (rule, label, git grep -E prefilter, exact pattern)
TOKENS = (
    ("aws", "AWS access key", r"(AKIA|ASIA)[0-9A-Z]{16}", r"(?<![A-Z0-9])(AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    ("github", "GitHub token", r"(gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})",
     r"\b(gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ("anthropic", "Anthropic API key", r"sk-ant-[A-Za-z0-9_-]{60,}", r"\bsk-ant-[A-Za-z0-9_-]{60,}"),
    ("openai", "OpenAI API key", r"sk-(proj-|svcacct-)?[A-Za-z0-9_-]{40,}", r"\bsk-(proj-|svcacct-)?(?!ant-)[A-Za-z0-9_-]{40,}"),
    ("stripe", "Stripe live key", r"(sk|rk)_live_[A-Za-z0-9]{20,}", r"\b(sk|rk)_live_[A-Za-z0-9]{20,}"),
    # real ones carry numeric workspace and user ids: xoxb-1234567890-1234567890-abc...; "xoxb-your-token" is a placeholder
    ("slack", "Slack token", r"xox[abprs]-[0-9]{8,}-[A-Za-z0-9-]{10,}", r"\bxox[abprs]-[0-9]{8,}-[A-Za-z0-9-]{10,}"),
    ("google", "Google API key", r"AIza[0-9A-Za-z_-]{35}", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("npm", "npm token", r"npm_[A-Za-z0-9]{36}", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("huggingface", "Hugging Face token", r"hf_[A-Za-z0-9]{34}", r"\bhf_[A-Za-z0-9]{34}\b"),
    ("sendgrid", "SendGrid API key", r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}", r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"),
    ("private-key", "Private key", r"-----BEGIN [A-Z ]*PRIVATE KEY", r"-----BEGIN (RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY"),
)
EXACT = [(rule, label, re.compile(exact)) for rule, label, _, exact in TOKENS]
# documentation and test fakes: AKIAIOSFODNN7EXAMPLE, sk_live_xxxxxxxx..., "your-key-here"
FAKE = re.compile(r"EXAMPLE|example|xxxxxxxx|XXXXXXXX|0000000000|your[_-]?(api[_-]?)?key|placeholder|dummy|redacted", re.I)
# Google API keys all start AIza: a Firebase web key is public by design (security rules guard the data), a Gemini,
# Maps or Cloud key is a real secret. Context that says "this ships to the browser" marks the first kind.
_PUBLIC_CONTEXT = re.compile(r"firebase|NEXT_PUBLIC_|VITE_|EXPO_PUBLIC_|REACT_APP_|PUBLIC_|measurementId|authDomain", re.I)
# lockfiles carry integrity hashes, not credentials; vendored code isn't yours to fix here
_SKIP_PATHS = (":(exclude,glob)**/*.lock", ":(exclude,glob)**/package-lock.json", ":(exclude,glob)**/pnpm-lock.yaml",
               ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/vendor/**", ":(exclude,glob)**/*.min.js")
# tests/, __tests__/, src/test/java, fixtures/, testdata/, mocks/; test_x.py, x_test.go, x.test.ts, x.spec.js, XTest.kt
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|specs?|fixtures?|testdata|test-data|mocks?|__mocks__|testing)(/|$)"
                        r"|(^|/)test_[^/]*$|_test\.\w+$|[._-](test|spec)\.\w+$|[a-z]Tests?\.(kt|java|cs|swift|scala)$")
# a real key body is base64 in 64-70 character lines; fixtures use "xx", "..." or nothing
_KEY_BODY = re.compile(r"[A-Za-z0-9+/=]{40,}")
TEST_CAP = 6.0  # a real token in a test is still live, but most are fakes: plan, not now
TEST_KEY_RISK = 2.5  # private keys in tests are nearly always generated fixtures


def in_tests(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def placeholder_key(text: str) -> bool:
    """A private-key header with no key under it ("-----BEGIN RSA PRIVATE KEY-----\\nxx"): a fixture or a docs snippet."""
    start = text.find("PRIVATE KEY")
    if start < 0:
        return False
    body = text[start + len("PRIVATE KEY"):start + 1500]
    end = body.find("-----END")
    return not _KEY_BODY.search(body[:end] if end >= 0 else body)


def _read(path: Path, limit: int = 1 << 20) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _git(repo: Path, *args: str, timeout: float = 120) -> str:
    r = proc.git(repo, *args, timeout=timeout)
    return r.stdout if r.returncode in (0, 1) else ""  # git grep: 1 = nothing found


def is_env_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return bool(_ENV_NAME.match(name)) and not _ENV_TEMPLATE.search(name)


def env_has_secrets(text: str) -> bool:
    return any(_SECRET_LINE.match(line) and not FAKE.search(line) for line in text.splitlines())


def _item(repo: Path, rule: str, rel: str, name: str, state: str, risk: float, headline: str, manual: str,
          line: int | None = None) -> Item:
    where = f"{rel}:{line}" if line else rel
    if in_tests(rel) and risk > TEST_CAP:
        risk = TEST_CAP
        headline += " (in test code: often a fixture, but a real one is still live)"
    return Item(Source.SECRET, f"{rule}:{where}", name, state, None, location=str(repo), lockfile=str(repo / rel),
                flag_risk=risk, manual=manual, kind="project",
                summary=Summary(headline=headline, upgrade_value="high" if risk >= 7 else "medium",
                                migration_effort="none", generated_by="heuristic"))


ROTATE = "Rotate the keys first (the service's dashboard issues new ones and revokes the old), then "


def env_items(repo: Path, tracked: list[str], history: list[str], untracked: list[str]) -> list[Item]:
    items = []
    for rel in tracked:
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        keys = env_has_secrets(text)
        items.append(_item(
            repo, "env-committed", rel, f"{rel} is committed", "committed", 9.0 if keys else 5.0,
            "Keys in this file are in the repository: anyone with a copy has them" if keys
            else "A .env file is in the repository; it holds no obvious keys yet",
            (ROTATE if keys else "") + f"stop tracking it (git rm --cached {rel}), add it to .gitignore, and commit an "
            ".env.example with empty values instead."))
    for rel in sorted(set(history) - set(tracked)):
        items.append(_item(
            repo, "env-history", rel, f"{rel} is still in git history", "in history", 7.5,
            "Deleted, but every earlier commit still contains it: if those were pushed, the keys are out",
            ROTATE + "decide whether to purge it from history (git filter-repo) and force-push. Rotating matters more: "
            "a purge can't recall copies already cloned."))
    for rel in untracked:
        items.append(_item(
            repo, "env-unignored", rel, f"{rel} isn't in .gitignore", "not ignored", 5.0,
            "One `git add .` away from committing these keys",
            f"Add {PurePosixPath(rel).name} (or .env*) to .gitignore, keeping an !.env.example exception if you share one."))
    return items


TEST_KEY = ("Keys in tests are usually generated just for the tests. If this one is, nothing to do (or generate it when "
            "the tests run). If the same key is used anywhere real, rotate it and keep it out of the repo.")


def key_file_items(repo: Path, tracked: list[str]) -> list[Item]:
    items = []
    for rel in tracked:
        head = _read(repo / rel, 8192)
        if (rel.endswith(".pem") and "PRIVATE KEY" not in head) or placeholder_key(head):
            continue  # a certificate (public) or a header with no key under it
        if in_tests(rel):
            items.append(_item(repo, "key-file", rel, f"{PurePosixPath(rel).name} is committed", "test fixture",
                               TEST_KEY_RISK, "A private key in test code: most likely a fixture", TEST_KEY))
            continue
        items.append(_item(repo, "key-file", rel, f"{PurePosixPath(rel).name} is committed", "committed", 8.0,
                           "A private key or key store is in the repository",
                           ROTATE.replace("keys", "key") + f"stop tracking it (git rm --cached {rel}) and add it to .gitignore."))
    return items


def parse_grep(text: str) -> list[tuple[str, int, str, str, bool]]:
    """`git grep -n` output -> (path, line, rule, label, public context), one per credential and file, fakes dropped.
    Values never leave here."""
    out, seen = [], set()
    for row in text.splitlines():
        path, sep1, rest = row.partition(":")
        line_no, sep2, content = rest.partition(":")
        if not (sep1 and sep2 and line_no.isdigit()):
            continue
        for rule, label, rx in EXACT:
            m = rx.search(content)
            if m and not FAKE.search(content) and (path, rule) not in seen:
                seen.add((path, rule))  # one finding per file and kind: the line points at the first
                out.append((path, int(line_no), rule, label, bool(_PUBLIC_CONTEXT.search(content) or _PUBLIC_CONTEXT.search(path))))
                break
    return out


GOOGLE_PUBLIC = ("Looks like a Firebase web key, which ships to every browser by design: Firebase security rules and App "
                 "Check protect the data, not the key. Make sure it's restricted to your domains in the Google Cloud "
                 "console; if it's actually a Gemini, Maps or Cloud key, rotate it and keep it out of the repo.")
GOOGLE_UNSURE = ("Check which API it's for in the Google Cloud console. A Firebase web key is public by design; a Gemini, "
                 "Maps or Cloud key is a secret: rotate it, restrict it, and move it into an environment variable.")


def token_items(repo: Path, hits: list[tuple[str, int, str, str, bool]], already: set[str]) -> list[Item]:
    items = []
    for path, line, rule, label, public in hits:
        if path in already:  # a committed .env or key file is already one finding
            continue
        if rule == "private-key":
            lines = _read(repo / path).splitlines()
            if placeholder_key("\n".join(lines[line - 1:line + 40])):
                continue
            if in_tests(path):
                items.append(_item(repo, f"token-{rule}", path, f"{label} in {path}", "test fixture", TEST_KEY_RISK,
                                   "A private key in test code: most likely a fixture", TEST_KEY, line))
                continue
        if rule == "google":
            items.append(_item(repo, f"token-{rule}", path, f"{label} in {path}", "committed", 3.0 if public else 6.0,
                               "A Google API key in a committed file" + (": probably a public Firebase web key" if public else ""),
                               GOOGLE_PUBLIC if public else GOOGLE_UNSURE, line))
            continue
        items.append(_item(repo, f"token-{rule}", path, f"{label} in {path}", "committed", 9.0,
                           f"A {label} is written into a committed file",
                           ROTATE.replace("the keys", "this key") + "move it into an environment variable (or an untracked "
                           ".env) and read it from there.", line))
    return items


def scan_repo(repo: Path) -> list[Item]:
    files = _git(repo, "ls-files", "-z").split("\0")
    env_tracked = [f for f in files if f and is_env_file(f)]
    keys = _git(repo, "ls-files", "-z", "--", *(f":(glob)**/{p}" for p in _KEY_FILES)).split("\0")
    added = _git(repo, "log", "--all", "--diff-filter=A", "--format=", "--name-only", "--",
                 ":(glob)**/.env", ":(glob)**/.env.*", timeout=300).splitlines()
    history = sorted({f for f in added if f and is_env_file(f)})
    others = _git(repo, "ls-files", "-z", "--others", "--exclude-standard", "--",
                  ":(glob)**/.env", ":(glob)**/.env.*").split("\0")
    untracked = [f for f in others if f and is_env_file(f)]
    grep_args = ["grep", "-I", "-n", "--no-color", "-E", *(a for _, _, pre, _ in TOKENS for a in ("-e", pre)), "--", ".",
                 *_SKIP_PATHS]
    hits = parse_grep(_git(repo, *grep_args, timeout=300))
    key_files = key_file_items(repo, [k for k in keys if k])
    return (env_items(repo, env_tracked, history, untracked) + key_files
            + token_items(repo, hits, set(env_tracked) | {Path(k.lockfile).relative_to(repo).as_posix() for k in key_files}))


def secret_items(_: Settings, repos: list[Path]) -> list[Item]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [i for chunk in pool.map(scan_repo, repos) for i in chunk]


def collect_secrets(s: Settings) -> list[Item]:
    return secret_items(s, find_repos(s.repo_roots, s.repo_depth))
