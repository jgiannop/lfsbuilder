#!/bin/bash
# lfsbuilder-trust: Portage's trust helper (PORTAGE_TRUST_HELPER) on systems
# that get updates from the distribution's own binary package server.
#
# Portage runs it as root before it downloads binary packages. It runs
# getuto, which keeps /etc/portage/gnupg (Gentoo's keys, trusted through a
# key of this machine's own), and then has that keyring trust the
# distribution's package signing key too: imported from
# /usr/share/openpgp-keys/lfsbuilder-binhost.asc and signed locally with
# getuto's key, exactly as getuto does for Gentoo's keys. Written by lfsbuilder.

set -euo pipefail

KEY=/usr/share/openpgp-keys/lfsbuilder-binhost.asc
export GNUPGHOME=/etc/portage/gnupg

/usr/bin/getuto "$@"
[ -f "$KEY" ] || exit 0

fingerprints=$(gpg --batch --show-keys --with-colons "$KEY" | awk -F: '$1 == "pub" { want = 1 } $1 == "fpr" && want { print $10; want = 0 }')
mine=$(cat "$GNUPGHOME/mykeyid")
# Every time: a newer copy of the key (a new expiry date, say) comes with an update.
gpg --batch --quiet --no-permission-warning --import "$KEY"
changed=0
for fpr in $fingerprints; do
    # Already signed by this machine's key: nothing more to do.
    if gpg --batch --with-colons --check-sigs "$fpr" 2>/dev/null | awk -F: -v k="${mine: -16}" '$1 == "sig" && $2 == "!" && $5 == k { found = 1 } END { exit !found }'; then
        continue
    fi
    gpg --batch --quiet --no-permission-warning --yes --no-tty --passphrase-file "$GNUPGHOME/pass" \
        --pinentry-mode loopback --quick-lsign-key "$fpr"
    changed=1
done
if [ "$changed" = 1 ]; then
    gpg --batch --quiet --no-permission-warning --check-trustdb
    chmod ugo+r "$GNUPGHOME/trustdb.gpg"
fi
gpgconf --kill all
