# GoS1 Infrastructure & Backup Context
# Companion to CLAUDE.md (which covers WattLab project specifics)
# Last updated: 2026-04-09

## Owner
Ben Schwarz (bs@ctoic.net / EURL CTO INNOVATION CONSULTING / SIREN 508109337)

## Disk Layout (April 2026)
```
/dev/nvme0n1p2  457G total, 213G used, 221G free (50%)

/home/gos     47 GB (but only ~3.5 GB real data — rest is caches/venvs)
  wattlab/       1.3 GB  (Git repo — github.com/greeningofstreaming/wattlab)
  .cache/       14 GB    (regenerable)
  .local/       18 GB    (mostly Python lib, regenerable)
  .venvs/        6.8 GB  (recreatable from requirements.txt)
  .ollama/       4.7 GB  (re-downloadable model weights)
  .claude/      91 MB
  .ssh/         28 KB    (critical — keys)
  .gnupg/       12 KB
  .config/     192 KB

/opt           27 GB
  rocm-6.2.4/  27 GB    (reinstallable)
  amdgpu/     106 MB    (reinstallable)
  teamviewer/  368 MB   (reinstallable)

/etc           14 MB    (system configs)
/var          8.8 GB    (logs, package cache)
```

## Other Users
dom, marisol, simon, tania — home dirs exist but unreadable by gos user.

## Nextcloud Backup (Hetzner Storage Share)
- **URL:** https://nx92576.your-storageshare.de
- **Plan:** NX11 base (possibly upgraded to 1 TB — verify at accounts.hetzner.com)
- **Username:** ben.flute@proton.me
- **Auth:** App password required for WebDAV/rclone (regular password rejected)
- **Custom domain planned:** cloud.ctoic.net (CNAME, not yet configured)
- **Version:** Nextcloud Hub 25 Autumn (32.0.6)
- **2FA:** Not configured (flagged in admin overview)
- **WebDAV endpoint:** https://nx92576.your-storageshare.de/remote.php/dav/files/ben.flute@proton.me/

### rclone Config (on GoS1)
- **Remote name:** `nextcloud`
- **Type:** webdav, vendor nextcloud
- **Commands:**
  - `rclone lsd nextcloud:` — list folders
  - `rclone about nextcloud:` — check used space
  - `rclone sync <src> nextcloud:<dest>/ --progress --skip-links`

### Backup Completed (April 2026)
Location: `nextcloud:GoS1-backup/`
- `ssh/` — SSH keys
- `gnupg/` — GPG keys
- `config/` — app settings
- `etc/` — system configs (minus root-only files: shadow, gshadow, ssh host private keys)
- `.claude.json`, `.gitconfig`, `.bashrc`, `.profile`

**Not backed up (by design):**
- wattlab/ — on GitHub
- .ollama, .venvs, .cache, .local, snap — reinstallable
- /opt/rocm-6.2.4, /opt/amdgpu, /opt/teamviewer — reinstallable
- Password hashes, SSH host private keys — shouldn't be in cloud

**Known quirks:**
- Snap mount files with `\x2d` in filenames rejected by Nextcloud WebDAV (backslash not allowed)
- Root-owned /etc files fail with permission denied under gos user — expected

**TODO:**
- [x] Cron job for recurring backup — `/etc/cron.d/wattlab-results-backup`, runs 03:30 daily, syncs `results/` to `nextcloud:GoS1-backup/wattlab-results/`, logs to `/var/log/wattlab-backup.log`
- [ ] Consider `rclone crypt` overlay for encryption before upload
- [ ] Replace raw /etc folder with selective tarball of key configs

## Nextcloud — Other Uses
- 1,031 deduplicated contacts (imported from contacts_final.vcf)
- Synced to Fairphone via DAVx⁵ (groups as per-contact categories)
- Synced to MacBook via native CardDAV (System Settings → Internet Accounts)
- CalDAV available but not yet configured

## Ben's Broader Stack
- **Phone:** Fairphone 6, e/OS (Murena)
- **Mail + Calendar:** Proton (migrating from Gmail)
- **Passwords:** Bitwarden (self-hosted eventually)
- **TOTP:** Leaning toward Aegis (offline, open-source)
- **Domain:** ctoic.net
- **DNS pending:** MX/SPF/DKIM for bs@ctoic.net + CNAME for cloud.ctoic.net
- **SSH from Fairphone:** Termux (F-Droid) + openssh

## Design Principles
- Minimize vendor concentration risk (including Proton monoculture)
- Prioritize data portability and exportability
- Prefer offline-first, open-source tools
- Favor clean architecture over patchwork workarounds
- Long-term sovereignty over short-term convenience
