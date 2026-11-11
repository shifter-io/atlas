# Deploying the siteapi without a gap (blue/green)

Every deploy of a new siteapi binary to the production API server follows this procedure.
Never restart the serving siteapi in place: a fresh process answers 503 (`starting`) on every
data route and on `/healthz` until its first build is done, about 100 to 140 s at today's volume.

Data changes need nothing here: a new calculation is built beside the serving version and
swapped in at once (`api/internal/server/server.go`, `current`). This page is only for a new binary.

The server's own details (how to log in, the paths, the public URL) are in the owner's private
runbook, which has the same steps. Nothing of them goes on GitHub.

## Layout

- **Two instances of one systemd unit template**, `siteapi@blue` and `siteapi@green`:
  - the same user, hardening and read-only database role (`proxy_api` on `proxy_reviews`);
  - `MemoryMax=7G` and `GOMEMLIMIT=6451MiB` (about 90 %, `api/README.md` "Caching: Memory");
  - the same flags (`--row-cache 512`, the database file);
  - per colour: its own loopback port (blue 8090, green 8091, from a small environment file),
    its own binary link (pointing at a commit's directory) and its own `--files-dir`, so neither
    writes the other's dataset file.
- **Exactly one colour serves:** the reverse proxy (Caddy) forwards to that colour's port.
  The other colour is stopped and disabled; the serving one is enabled at boot.
- Each binary lives in its own directory named by its full commit. Keep the last two or three
  for rollback; never delete one a colour's link points at.

## Before a deploy

1. **Build** `siteapi` from the merged `main` commit, never on the owner's machine (the tor1
   staging manager): a clean clone, `GOTOOLCHAIN=local GOFLAGS=-mod=readonly CGO_ENABLED=0
   GOOS=linux GOARCH=amd64 go build -trimpath ./cmd/siteapi`. `go version -m` must show
   `vcs.revision` = the commit and `vcs.modified=false`. Record its SHA-256.
2. **Copy** it into a new directory beside the old binaries and check the SHA-256 on the server.
3. **Migrations:** if the commit adds one (`api/internal/store/migrations/`), run `siteapi migrate`
   with the schema owner's credentials first (they never stay on the server). An older binary
   still serves on a database migrated by a newer one.
4. **Memory:** both colours run during a switch. Check that
   the idle colour's first-build peak (at most 3 GB, SITEAPI-MEM-1)
   + the serving colour's current memory (`systemctl show <unit> -p MemoryCurrent`)
   + 2 GB for the system fits in the server's total memory (`free -b`).
   If not, **stop and report; do not deploy over the limit.**
5. Start an **outside probe** (another host, through the public URL): `/v1/version` and
   `/v1/rankings?range=24h` every second, logging each status code, until after step 6 below.

## The deploy

1. **Point the idle colour's link** at the new commit's directory.
2. **Start the idle colour** (`systemctl start siteapi@<idle>`). The serving colour keeps serving.
3. **Wait on the idle colour's own port** (loopback, not through the proxy) until all of these hold:
   - `/healthz` has `"status":"ok"`, `"unserved":[]` and `"lastError":null`;
   - `/healthz` `binary.revision` is the new commit;
   - each of these answers 200: `/v1/version`, `/v1/rankings?range=24h`, `/v1/networks/shifter`,
     `/v1/overlap?range=30d`, `/v1/search-index`, and `/v1/dataset` (a 302 whose target answers 200).

   If it does not get there, stop the idle colour and investigate: nothing has changed for clients.
4. **Switch the proxy:** change the `reverse_proxy` upstream to the idle colour's port, then
   `caddy validate`, then `systemctl reload caddy`. The reload is graceful: Caddy keeps its
   listening socket, and requests in flight finish. Keep a copy of the previous config.
5. **Check through the public URL:** `/healthz` names the new revision, and the probe shows only 200.
6. **Drain, then stop the old colour:** wait 60 s after the reload, then
   `systemctl stop siteapi@<old>` and `systemctl disable siteapi@<old>`,
   `systemctl enable siteapi@<new>`. Read both colours' `MemoryPeak` before stopping.
7. **Stop the probe** a little after step 6 and record: the requests, the non-200 count (must
   be 0), the time of the switch and both peaks of memory.

Expected after a switch: `/v1/version`'s `revision` changes even when the data did not, because
each build writes a new dataset file. Open pages refetch once. An old dataset file name answers
a 302 to the current file, never a 404.

## Rollback

- **Before step 6** (the old colour still runs): set the proxy upstream back to the old colour's
  port and `systemctl reload caddy`. Then stop the new colour.
- **After step 6:** start the old colour (its link, binary and unit are unchanged), wait for its
  checks as in step 3, switch the proxy back and reload, then stop the new colour after 60 s.
- Migrations are never rolled back: they only add to `site`, and an older binary checks only the
  migrations it embeds.

## Never

- Restart the serving colour, or run a deploy that stops it before the other colour serves.
- Start both colours on the same port or with the same `--files-dir`.
- Deploy a commit that is not on `main`, or a binary built from a modified tree.
