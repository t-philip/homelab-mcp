# Publishing rule — what may and may not appear in this repo

`homelab-mcp` connects to real infrastructure — Proxmox, NetBox, Grafana,
InfluxDB, AdGuard, hosts over SSH. Its docs, `DESIGN_SPEC.md`, example config
and tests all describe how that works. That is exactly how a specific
environment's host names, IP addresses and endpoint URLs can end up in a
public repository.

## The rule

> **A comment, an example, or a fixture may describe behaviour and reasoning.
> It may not describe a specific installation.**

The test to apply to any line you are about to write:

> *Could this have been written by someone who has never seen the author's
> network?*

## Specifics

1. **Code comments and docstrings** — general behaviour only. Never a real
   host name, internal DNS name, IP address, endpoint URL, or SSH alias.
2. **Example config (`homelab-mcp.example.toml`)** — every `base_url`,
   `hosts` entry, endpoint URL and `path` uses a `.invalid` / `.example`
   hostname or an RFC 5737 documentation IP. Every adapter ships
   `enabled = false`.
3. **Test fixtures — the fiction window.** Any absolute date in test data
   uses **a year before 2002**, and nothing else. Relative fixtures (built
   from `now - timedelta(...)`) stay relative — they carry no calendar fact
   and pinning them would break the test. Host names in fixtures are
   `node-a` / `node-b` / `pmx-1`; addresses are in `192.0.2.0/24` and
   `198.51.100.0/24`; free text is `"Test Data ..."`.
4. **`docs/DESIGN_SPEC.md`** — describes the design of this server, not the
   author's deployment. No real host inventory, no real endpoint, no dated
   observation.
5. **Network examples** — RFC 5737 documentation ranges only
   (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) plus loopback.

## Where the specific configuration goes instead

Into a private deployment repository, which is not published. The real host
list, real endpoint URLs, the read-only Proxmox token reference and the
persona files live there. This repo is the generic engine; the deployment is
private.

## Checking

Two halves, and **both** are required.

**Structural** — `scripts/check_privacy.sh`, run automatically by the
`pre-push` hook in the tracked `.githooks/` directory (`.git/hooks` is never
cloned). Activate it once per clone:

```sh
git config core.hooksPath .githooks
```

It checks the *diff*, not the tree, for internal host names, real IPs,
emails, dated prose, absolute dates at or after 2002, non-placeholder
free-text fields, quoted log-note-shaped phrases, and the patterns in
`.privacy-denylist` (SSH aliases, internal DNS suffixes, real endpoint
URLs). `.github/workflows/check-privacy.yml` runs the same script
server-side, since the hook is bypassable with `git push --no-verify`.

**Semantic** — a read of the diff by someone, or something, with no memory
of writing it, answering the one question above.

**A pass from the script does not mean the diff is safe to publish.** It
means the cheap half found nothing.
