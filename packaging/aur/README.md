# The AUR package

`PKGBUILD` and `.SRCINFO` for `kramer-vs44-remote-control` on the AUR. The package installs the
**desktop window only** — the HTTP service is meant to be a long-lived daemon and the container
image is the right shape for that, so shipping a systemd unit nobody asked for would be
maintenance without a user.

## What it installs

```
/usr/bin/kramer-gui                       a two-line launcher
/usr/lib/kramer-vs44/*.py                 the three modules, side by side
/usr/lib/kramer-vs44/packaging/kramer.png the window icon, read at runtime
/usr/share/applications/…desktop          the menu entry
/usr/share/icons/hicolor/…                scalable and 256 px
```

The three modules must stay in one directory: `kramer_gui.py` imports its siblings by plain name
and relies on Python putting the script's own directory first on `sys.path`. This is not a Python
distribution and there is no package to install.

Settings land in `~/.config/kramer-vs44/`, never beside the program — `kramer_paths.config_path()`
only uses a file next to the program when one already exists, and `/usr/lib` will not have one.
That is checked, not assumed: see `tests/test_paths_offline.py`.

## Versions are not edited by hand

`pkgver` and `sha256sums` are rewritten by `.github/workflows/release.yml` when a version tag is
pushed, which is also when it publishes here. That is deliberate: the numbers in this file are only
correct for one release, and a human updating them is a human who will eventually forget.

**Note the consequence:** the values committed here point at whatever release was current when they
were last written, and the tarball of an *older* release will not contain files added since. If you
want to build the package by hand, build it from the current tree as below rather than from the
committed `source=` line.

## Building it locally

`makepkg` refuses to run as root, so this needs an ordinary user with `base-devel`, and `tk`.

```bash
# A tarball shaped like GitHub's, from the working tree rather than from a tag.
tar --exclude-vcs --exclude=./tests --exclude=./.github \
    --transform 's|^\./|kramer-vs44-remote-control-0.2.0/|' \
    -czf /tmp/kramer-vs44-remote-control-0.2.0.tar.gz -C /path/to/checkout .

mkdir -p ~/build && cd ~/build
cp /tmp/kramer-vs44-remote-control-0.2.0.tar.gz .
cp /path/to/checkout/packaging/aur/PKGBUILD .
sed -i 's#^source=.*#source=("$pkgname-$pkgver.tar.gz")#' PKGBUILD
sed -i "s#^sha256sums=.*#sha256sums=('SKIP')#" PKGBUILD

makepkg                                   # build
namcap PKGBUILD *.pkg.tar.zst             # lint
bsdtar -tf *.pkg.tar.zst                  # look at what is actually inside
sudo pacman -U *.pkg.tar.zst              # install, if you want to run it
```

Regenerate `.SRCINFO` after any change to `PKGBUILD` — the AUR reads that file, not the script:

```bash
makepkg --printsrcinfo > .SRCINFO
```

## namcap

No errors. Six warnings, all expected, all explained in a comment at the top of `PKGBUILD`. Read it
before treating any of them as a defect: two are this package's own modules, two are the optional
pyserial import, one is Tk being loaded rather than linked, and one is `sh`.

## Publishing

The AUR is a public channel with an implied promise of maintenance — an abandoned package is worse
than no package. Publication is automated so that promise costs nothing per release. Without the
secret below the release workflow skips the step and says so, rather than failing.

### One-time setup

**1. A dedicated key, with no passphrase.** Dedicated because the private half goes into GitHub and
should be revocable on its own; no passphrase because nothing can type one in CI.

```bash
ssh-keygen -t ed25519 -C "github-actions -> aur" -N "" -f ~/.ssh/aur_ci
```

**2. Give the AUR the public half.** Log in at <https://aur.archlinux.org>, then *My Account* → *SSH
Public Key*, and paste the contents of `~/.ssh/aur_ci.pub`. The AUR accepts several keys, so this
does not disturb the one you already use.

**3. Give GitHub the private half.** Read from the file rather than typing it on a command line,
where it would land in your shell history:

```bash
gh secret set AUR_SSH_PRIVATE_KEY --repo Piero-93/kramer-vs44-remote-control < ~/.ssh/aur_ci
```

**4. Optional, and worth it: pin the host key.** By default the workflow accepts whatever
`aur.archlinux.org` offers on first contact, which authenticates nothing. If you have already
verified that host from your own machine, publish what you verified:

```bash
gh variable set AUR_KNOWN_HOSTS --repo Piero-93/kramer-vs44-remote-control \
  --body "$(ssh-keygen -F aur.archlinux.org -f ~/.ssh/known_hosts | grep -v '^#')"
```

### Checking it worked

The first tag after the secret exists publishes the package. The AUR repository does not need to be
created first: cloning a package that does not exist yet gives an empty repository, and the push
creates it — but that first run is the one to watch, and the *Push to the AUR* step prints what it
did either way.

If you ever want it gone: `ssh aur@aur.archlinux.org` offers no delete, so ask on the AUR web
interface. Removing the GitHub secret stops the automation without touching what is published.
