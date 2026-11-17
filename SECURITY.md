# Security

## Reporting a vulnerability

Please report it privately. Do not open a public issue.

Use GitHub's private vulnerability reporting: open this repository's **Security** tab and choose
**Report a vulnerability**. Include the commit shown in the sidebar, your platform, and the steps that
show the problem. Remove anything private from what you paste: names, paths, tokens.

Bosun is provided as is. Reports are welcome, but no reply or fix date is promised. If you want credit,
say how you would like to be named.

## What is in scope

- Another account, a container, a sandboxed app or a web page getting past the session cookie, the page
  token, the `Host` and `Origin` checks, or the agent signature.
- A way to make Bosun run a command it should not: a shell string, a repository's own git config or hooks,
  an unvalidated id reaching an upgrade command.
- Text from outside (release notes, package metadata, model output, a web page) acting as an instruction,
  being drawn as HTML, or making the assistant or an agent act.
- Self-update installing code that is not a signed release, or moving to an older one.
- Bosun's files in its data folder being read or changed by another account.
- Anything that sends your inventory off the machine beyond the public lookups the README describes.

## What is not in scope

- Problems that need an attacker who already runs code as you. That attacker can read the key file and the
  database without Bosun.
- Vulnerabilities in the software Bosun lists, or in Homebrew, `uv`, `git` or Claude Code themselves.
- Windows and Linux behaviour that only differs from macOS. Those systems are experimental.
- Denial of service against a dashboard that listens on `127.0.0.1` only.

## Support

macOS is the supported platform. Windows and Linux are experimental: the test suite runs on them in CI and
nothing more is promised. A running Bosun updates itself only to signed
release tags (`vX.Y.Z`), so a fix reaches people when a new tag is published.

## Threat model

Bosun is a local tool for one person. It reads what is installed, asks public services about it, and can run
updates. The server binds to `127.0.0.1` and does not bind anywhere else.

**What Bosun trusts**

- The local user. Anyone who can read `session.key` in the data folder can sign in, and can sign agent
  requests. The folder is readable by your account only.
- Signed release tags. Self-update installs a release only if its tag and every commit up to it are signed
  by a key in `trusted_signers`, as read from the commit already installed.
- Homebrew's checksums for what Homebrew downloads. For apps, Bosun adds its own check that the new copy is
  signed by the same developer as the old one, and keeps the old copy for 14 days (less if the kept copies together pass 20 GB, but never less than a day).
- The operating system's own updater and signature tools (`codesign`, Gatekeeper).

**What Bosun treats as untrusted**

- Release notes, changelog pages and update feeds. They are fetched over https only, never from an address on
  this computer or network, and never more than 2 MB. They are shown with `textContent` and never as HTML.
- Package metadata and names from registries and package managers. Ids are validated, and commands are built
  as argv lists, never through a shell.
- Model output. A model's reading of release notes never authorises an unattended major-version install, and
  nothing a release note says can make the assistant act. Only an approval card you click can.
- Web pages in your browser. Every request needs the session cookie, and every change needs the page token
  and a same-origin check.
- Repositories on disk. Every git call switches off `core.fsmonitor`, hooks and `git log`'s signature check
  (`log.showSignature`). Fix branches are the exception, being hardened. Preparing one (`status`, `add`,
  `commit`, `worktree add`) can still run a repository's filter and diff programs, and `commit` the signing
  program its config names (`gpg.program`, `gpg.ssh.program`, `gpg.x509.program`) when it sets
  `commit.gpgsign`. Opening its pull request
  (`git push`, `gh pr create`) can still run a program the repository's config names
  (`remote.<name>.receivepack`, `core.sshCommand`, `credential.helper`), and `gh` runs git without Bosun's
  protections. That needs a person's click (the dashboard's button, or an approval for the assistant), and an
  agent can do it only once you turn that on.
- Other software writing to Bosun's data folder. Databases from another checkout are not migrated, and
  the database is copied before every migration.
- Other local agents. They get no cookie, reach actions only, and may not quit apps, open system updaters,
  push branches or change Bosun's own limits unless you turn that on.

**Known limit.** The Branch + test fix has no operating-system sandbox on Windows, or on Linux without
`bwrap` and `socat`. A dependency's install script or test then runs with your access.
