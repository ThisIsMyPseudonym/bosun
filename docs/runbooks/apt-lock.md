# apt upgrade fails: the lock is held

**Looks like:** `Could not get lock /var/lib/dpkg/lock-frontend` or `dpkg frontend lock` in a failed job's output.

**Why:** only one package manager can run at a time. Most often the system's automatic security updates
(`unattended-upgrades`) or the software center is running.

**Fix:** wait a few minutes and upgrade again. Nothing is broken and nothing was half-installed: apt stopped
before it changed anything. Do not delete lock files and do not kill `dpkg`: that is how a package database
gets corrupted.

**Check:** `ps aux | grep -E 'apt|dpkg|unattended'` shows what holds the lock.

On Windows, packages inside WSL are upgraded with `apt-get install --only-upgrade` as root inside the distro and
can hit the same lock.
