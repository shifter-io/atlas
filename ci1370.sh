set -u
export PATH=/opt/proxy-index/go/bin:$PATH
PGV=17.11.1; SHA=aa8e2e97574bd789119ac01f1dddcd7a038c84e5c308d7b753018618258f0fbd
cd /tmp && curl -fsSL --retry 3 -o pg.tgz "https://github.com/theseus-rs/postgresql-binaries/releases/download/$PGV/postgresql-$PGV-x86_64-unknown-linux-gnu.tar.gz" && echo "$SHA  pg.tgz" | sha256sum -c --quiet - && mkdir -p pgx && tar -C pgx --strip-components=1 -xzf pg.tgz || { echo PGFAIL; exit 9; }
id pg >/dev/null 2>&1 || useradd -m pg
chmod -R a+rX /tmp/pgx; mkdir -p /tmp/pgd && chown pg /tmp/pgd
echo pw123 > /tmp/pw; chmod 644 /tmp/pw
su pg -c "/tmp/pgx/bin/initdb -D /tmp/pgd/data -U postgres --pwfile=/tmp/pw --auth=scram-sha-256 --encoding=UTF8 --locale=C --data-checksums >/dev/null && /tmp/pgx/bin/pg_ctl -D /tmp/pgd/data -o '-p 5433 -k /tmp/pgd -c listen_addresses=127.0.0.1' -l /tmp/pgd/log -w start" || { echo PGSTARTFAIL; exit 9; }
printf '[postgres]\nhost = "127.0.0.1"\nport = 5433\nuser = "postgres"\npassword = "pw123"\ndbname = "postgres"\n' > /tmp/pg-admin.toml; chmod 600 /tmp/pg-admin.toml
export PG_BIN=/tmp/pgx/bin PG_TEST_ADMIN_FILE=/tmp/pg-admin.toml PG_TEST_REQUIRED=1
WD=$(ls -d "$OLDPWD" 2>/dev/null); cd "$OLDPWD" 2>/dev/null; cd api || cd */api || { pwd; ls; exit 8; }
echo "=== 20x race"
go test -race -count=20 -v -run '^TestManyUnknownKindsAnswerFast$' ./internal/server/ 2>&1 | grep -E '^(--- |ok|FAIL|panic)' | sort | uniq -c
echo "=== canonical tests (race, db)"
go test -race -count=1 -v -run 'Canonical|Redirect|GuardHands|TooManyValues' ./internal/server/ ./internal/build/ 2>&1 | grep -E '^\s*(--- |ok|FAIL|panic)' | grep -vc PASS_NEVER
go test -race -count=1 -v -run 'Canonical|Redirect|GuardHands|TooManyValues' ./internal/server/ ./internal/build/ 2>&1 | grep -E '^\s*(--- (FAIL|SKIP)|ok|FAIL|panic)'
echo "=== full server+build, no race"
go test -count=1 -skip '^(TestPeakRSS|TestServeFromDatabase|TestPageLatency|TestFirstCoverageSearchAfterABuild|TestFloodKeepsCachedAnswersFast)$' ./... 2>&1 | grep -E '^(ok|FAIL|---|panic)' | tail -40
echo "=== bench"
go test -run '^$' -bench . -benchmem -count=1 ./internal/server/ 2>&1 | grep -i canon
