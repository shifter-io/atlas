#!/usr/bin/env bash
# Prints the steps that give one database server a certificate for its name or address, so every
# client can connect with sslmode=verify-full (SEC-DBTLS-2; the operator's runbook, kept outside the repository).
# It runs nothing itself: the owner, or an operator with the owner's login, runs each block where
# it says. The private CA lives in an owner-only folder on the operator's Mac and never on a
# server; the database server makes its own key, which never leaves it (as the browser hosts do,
# SEC-MINI-KEYS-1). Only the signing request and the certificates travel.
#
#   bash scripts/db-tls-setup.sh <database server name or address> [--ca-dir <folder on the Mac>]
set -euo pipefail

usage() { echo "usage: db-tls-setup.sh <database server name or address> [--ca-dir <folder on the Mac>]" >&2; exit 2; }

SERVER=${1:-}
[ $# -gt 0 ] && shift
CA_DIR='~/atlas-db-ca'
while [ $# -gt 0 ]; do
	case $1 in
	--ca-dir) CA_DIR=${2:-}; [[ $CA_DIR =~ ^[A-Za-z0-9_./~][A-Za-z0-9_./~-]*$ ]] || usage; shift 2 ;;
	*) usage ;;
	esac
done

# The certificate names the server as the clients' `host` does: an address as IP, a name as DNS.
if [[ $SERVER =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || [[ $SERVER =~ ^[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*$ ]]; then
	SAN="IP:$SERVER"
	ADDR=$SERVER
elif [[ $SERVER =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]; then
	SAN="DNS:$SERVER"
	ADDR="\$(getent ahosts $SERVER | awk 'NR == 1 {print \$1}')"
else
	usage
fi
STEM=atlas-db-$(date -u +%Y%m%d)

cat <<EOF
# Database TLS for $SERVER (SEC-DBTLS-2). This script ran nothing: run each block where it says.
# The owner, or an operator with the owner's login, installs the certificate on the database server.

## 1. On the operator's Mac, once for all database servers: the private CA in $CA_DIR.
#     ca.key never leaves this folder (keep it backed up offline); only ca.crt goes to the servers.
#     Skip this block when $CA_DIR/ca.crt exists: every server and client shares that CA.
install -d -m 700 $CA_DIR && cd $CA_DIR
test ! -e ca.key || { echo "a CA is here already: keep it"; exit 1; }
cat >ca.cnf <<'CNF'
[req]
distinguished_name = dn
[dn]
[db_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
CNF
(umask 077 && openssl ecparam -name prime256v1 -genkey -noout -out ca.key)
openssl req -x509 -new -key ca.key -config ca.cnf -extensions db_ca -subj "/CN=atlas-db-ca" -days 3650 -sha256 -out ca.crt

## 2. On the database server, as the postgres system user, in the data directory
#     (psql -Atc 'SHOW data_directory'): its own key and a signing request.
test ! -e $STEM.key || { echo "$STEM.key exists: keep it"; exit 1; }
(umask 077 && openssl ecparam -name prime256v1 -genkey -noout -out $STEM.key)
openssl req -new -key $STEM.key -subj "/CN=$SERVER" -out $STEM.csr
openssl dgst -sha256 $STEM.csr
#     Copy only $STEM.csr to $CA_DIR on the Mac.

## 3. On the Mac: sign it for $SAN, server authentication only, 825 days.
cd $CA_DIR
openssl dgst -sha256 $STEM.csr   # must equal the SHA-256 the database server printed
printf '%s\n' 'basicConstraints = critical,CA:FALSE' 'keyUsage = critical,digitalSignature' \\
    'extendedKeyUsage = serverAuth' 'subjectAltName = $SAN' >$STEM.ext
openssl x509 -req -in $STEM.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -sha256 -extfile $STEM.ext -out $STEM.crt
openssl verify -CAfile ca.crt $STEM.crt
#     Copy only $STEM.crt back into the data directory. Before day 825, repeat steps 2 to 4.

## 4. On the database server: the certificate in place and the PostgreSQL ssl_* settings.
chmod 644 $STEM.crt
psql -v ON_ERROR_STOP=1 <<'SQL'
ALTER SYSTEM SET ssl = on;
ALTER SYSTEM SET ssl_cert_file = '$STEM.crt';
ALTER SYSTEM SET ssl_key_file = '$STEM.key';
ALTER SYSTEM SET ssl_min_protocol_version = 'TLSv1.2';
ALTER SYSTEM SET ssl_ciphers = 'HIGH:!aNULL:!MD5';
SELECT pg_reload_conf();
SQL
#     In pg_hba.conf every line for another machine starts with hostssl, none with host or
#     hostnossl; after editing, psql -c 'SELECT pg_reload_conf()'. This must print no row:
psql -Atc "SELECT line_number, type, address FROM pg_hba_file_rules WHERE type IN ('host', 'hostnossl') AND address NOT IN ('127.0.0.1', '::1')"
psql -Atc "SELECT name, setting FROM pg_settings WHERE name IN ('ssl', 'ssl_cert_file', 'ssl_key_file', 'ssl_min_protocol_version')"

## 5. The clients. From the Mac, copy only the CA certificate (never ca.key) to the Canada server:
#     scp $CA_DIR/ca.crt <Canada server>:.config/atlas/db-root.crt
#     Then on the Canada server, for production and for staging, on the commit each already runs
#     (it must hold SEC-DBTLS-2; the database files there must name the server as "$SERVER"):
for env in production staging; do
    bash ~/atlas/\$env/src/scripts/server-setup.sh \$env --ref "\$(git -C ~/atlas/\$env/src rev-parse HEAD)"
done
#     Each run installs the certificate as ~/.config/atlas/<env>/db-root.crt and gives each database
#     file sslmode = "verify-full" and that sslrootcert. A service reads it at its next start.

## 6. Check C2, on the Canada server, for each environment (<env> = production, then staging).
#     a. Both logins connect with verify-full: each report's "server" shows "ssl": true.
~/atlas/<env>/venv/bin/atlas db-server check --database-file ~/.config/atlas/<env>/database-api.toml
~/atlas/<env>/venv/bin/atlas db-server check --database-file ~/.config/atlas/<env>/database.toml
#     b. A client that trusts other CAs is refused ("certificate verify failed"):
PGCONNECT_TIMEOUT=5 ~/atlas/bin/psql "host=$SERVER dbname=postgres user=atlas_c2 sslmode=verify-full sslrootcert=system" -c 'SELECT 1' </dev/null
#     c. Another name for the same server is refused ("does not match host name"):
PGCONNECT_TIMEOUT=5 ~/atlas/bin/psql "host=atlas-c2.invalid hostaddr=$ADDR dbname=postgres user=atlas_c2 sslmode=verify-full sslrootcert=\$HOME/.config/atlas/<env>/db-root.crt" -c 'SELECT 1' </dev/null
EOF
