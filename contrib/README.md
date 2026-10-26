# Proctor scheduling notes
#
# systemd hosts:
#   sudo proctor schedule --install --at 03:30
#
# Linux with cron.d (Debian/Ubuntu/Fedora/Arch/Alpine/Gentoo/Void):
#   sudo proctor schedule --install --at 03:30 --type cron
#
# FreeBSD/OpenBSD/NetBSD/DragonFly:
#   Add to /etc/crontab (as root):
#   30 3 * * * root /usr/local/bin/proctor backup --quiet
#
#   OpenBSD: crontab -e for root, or /etc/crontab depending on the base system.
#
# OpenRC (Alpine/Gentoo) and runit (Void) do not have a portable one-shot timer;
# use cron (vixie-cron, cronie, dcron) or contrib/runit/proctor-backup.
#
# The scheduled job normally stops Prosody briefly. If that is unacceptable:
#   30 3 * * * root /usr/local/bin/proctor backup --live --quiet


## Example landing page

`contrib/sonder.guru/` contains a self-contained landing page and an example nginx vhost for a host that also runs Proctor/Prosody.
