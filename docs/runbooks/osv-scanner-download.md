# osv-scanner could not be downloaded or updated

**Looks like:** a refresh note "osv-scanner <version> kept (update failed: ...)", or the error "osv-scanner
could not be installed: ...". The reason may be a network error, "checksum mismatch", "implausibly large" or
"the download reports version ...".

**Why:** when no osv-scanner is installed (Homebrew or `PATH`), Bosun keeps its own copy in `bin/`, at the
version and SHA-256 written in `src/bosun/scanner.py`. A download must match that hash, then run and report
that version, before it replaces the copy in `bin/`.

**Effect:** if a working copy was there, it stays and scans carry on with it. Only with no copy at all do
project and image scans fail for that refresh.

**Fix:**
- A network error: retry with `bosun update-scanner`.
- A checksum or version mismatch: do not work around it. The file is not the one this Bosun commit pins.
  Retry later; if it persists, report it. A newer osv-scanner arrives with the Bosun commit that moves the pin.
- To manage it yourself: install osv-scanner with Homebrew or put it on `PATH`, or set `BOSUN_OSV_SCANNER` to a
  copy, or `BOSUN_OSV_AUTO_UPDATE=0` to leave `bin/` alone.
