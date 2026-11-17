# Gives a [postgres] database file sslmode = "verify-full" and sslrootcert = root (SEC-DBTLS-2):
# it drops the file's own sslmode and sslrootcert lines and writes both right after [postgres].
# Every other line, the password's too, is copied unchanged.
#
#   awk -v root=<root certificate> -f db-verify-full.awk <database file>
/^[ \t]*(sslmode|sslrootcert)[ \t]*=/ { next }
{ print }
/^[ \t]*\[postgres\][ \t]*(#.*)?$/ {
	print "sslmode = \"verify-full\""
	print "sslrootcert = \"" root "\""
}
