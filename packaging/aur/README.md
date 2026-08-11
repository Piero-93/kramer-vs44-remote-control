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
than no package. Publication is automated so that promise costs nothing per release, but it needs
one secret set up once: an SSH key with write access to the AUR repository, held as
`AUR_SSH_PRIVATE_KEY` in the GitHub repository's secrets. Without it the release workflow skips the
step and says so rather than failing.
