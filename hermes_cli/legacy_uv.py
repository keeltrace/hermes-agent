"""The pre-PM ``uv``/``uvx`` family an old install left in ``$HERMES_HOME/bin``.

PM stages uv inside its own store and keeps it off PATH (``Uv.on_path`` is
False), so these binaries are dead weight — and load-bearing while they last:
on Windows ``install.ps1``'s ``Set-LauncherUserPath`` PREPENDS that directory
to the User PATH, so a leftover ``uv.exe`` shadows the user's own uv in every
shell (#101269), and ``tools/environments/local.py`` appends the same directory
to the agent terminal's PATH, where it shadows it again.

One implementation is shared by the uninstaller, ``hermes update``'s self-heal
and ``hermes doctor --fix`` so the three cannot drift.
"""

import errno
import os
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.colors import Colors, color


def _log_warn(msg: str) -> None:
    print(f"{color('⚠', Colors.YELLOW)} {msg}")


#: The pre-PM uv family an old install/`uv self` pair dropped in ``$HERMES_HOME/bin``.
LEGACY_MANAGED_UV_NAMES = ("uv", "uvx", "uv.exe", "uvx.exe")

#: Bounded wait on PM's install lock before touching those binaries: legacy migration is a
#: shared mutation under the one install lock, fail-closed on timeout. Module-level so
#: tests can shorten it.
_LEGACY_UV_LOCK_TIMEOUT = 10.0


def _pm_install_lock():
    """PM's shared install lock, but only where a store already exists to serialize with.

    The root is ``writable_store_root()`` because that is the one PM's installs lock:
    ``ensure()`` and ``_InstallOperation.lock()`` both build ``Store(writable_store_root())``.
    ``store_root()`` alone would be a *different* file on a sealed payload install (where it
    resolves inside the read-only payload) — locking it would serialize against nobody.

    Taking the lock when the store is already absent would create ``<store>/.install.lock`` —
    i.e. the store dir — during an uninstall. A concurrent removal after this probe can still
    race with ``install_lock()``; that window is harmless apart from recreating the empty root.
    """
    from contextlib import nullcontext

    from pm.paths import writable_store_root
    from pm.store import Store

    root = writable_store_root()
    if not root.is_dir():
        return nullcontext()
    return Store(root).install_lock(timeout=_LEGACY_UV_LOCK_TIMEOUT)


def bin_escapes_home(hermes_home: Path) -> bool:
    """True when ``<home>/bin`` resolves anywhere other than the home's own ``bin``.

    ``is_file()``/``unlink()`` traverse a directory PARENT: with ``<home>/bin`` a symlink (or
    a Windows junction/reparse point) into an ordinary user bin, deleting ``<home>/bin/uv``
    deletes the USER's real file — the legacy-uv cleanup must never intentionally target a user's
    own uv elsewhere. Identity is realpath equality, not a string prefix: a
    symlinked *home* still resolves to its own real ``bin`` and reads as anchored; only a bin
    that escapes the home reads as escaping. Absent/not-a-directory is not an escape (there
    is nothing there to anchor).
    """
    bin_dir = hermes_home / "bin"
    if not bin_dir.is_dir():
        return False
    return os.path.normcase(os.path.realpath(bin_dir)) != os.path.normcase(
        os.path.join(os.path.realpath(hermes_home), "bin")
    )


@dataclass(frozen=True)
class _BinAnchor:
    """The ``<home>/bin`` a deletion run is anchored to.

    ``fd`` is the POSIX directory handle the leaf unlinks are relative to, so the
    directory identity is pinned when it is opened and a later swap of the *path*
    cannot redirect them. ``home_fd`` is the retained parent handle ``fd`` was
    acquired through, kept so the chain stays rooted in the home that was verified.
    There is deliberately no "no anchor but still delete" representation: platforms
    that cannot retain these handles fail closed in :func:`_open_home_bin`. An
    *absent* ``bin`` is not representable either — that returns ``None`` instead,
    because "nothing was acquired" means "delete nothing".
    """

    fd: int | None
    home_fd: int | None = None


def _open_home_bin(hermes_home: Path) -> _BinAnchor | None:
    """Anchor ``<home>/bin`` for the deletions below; ``None`` when there is no directory.

    The home is the identity to pin, not the bin: a check that resolves ``<home>/bin`` and a
    later ``os.open(<home>/bin)`` are two independent lookups, and between them a swap can make
    the *path* name a different directory than the one that was verified. So the home itself is
    opened first (following a symlink, which is how a legitimately symlinked home works) and
    ``bin`` is then acquired relative to that retained handle with ``O_NOFOLLOW``. Leaf
    inspection and the unlinks then run on ``dir_fd``s inside that one anchored chain, so no
    later rename of the home path can re-point them.

    Refuses (raises ``OSError``) before the first unlink when ``bin`` escapes the home
    (:func:`bin_escapes_home`) and when the platform cannot retain a trustworthy deletion
    capability at all — Windows has no ``dir_fd``, so a post-check junction swap stays
    unclosable there. Failing closed is the only safe answer: leaving ``fd=None`` would
    authorize exactly the path-based unlink the anchor exists to prevent.
    """
    bin_dir = hermes_home / "bin"
    if os.name != "posix":
        # Without dir_fd every unlink would go back through the path, i.e. through whatever
        # the post-check swap put there. Warn-and-continue is the vulnerability; do nothing.
        raise OSError(
            f"no directory-relative deletion capability on {os.name}; refusing to delete "
            f"through {bin_dir} by path"
        )
    # Order matters twice over, and the whole repair is the order.
    #
    # 1. Record the home's identity BEFORE retaining a handle. A path lookup and a later
    #    open of the same path are independent resolutions, and a swap between them makes the
    #    handle describe a directory nobody vetted. Statting first turns "open whatever the
    #    path says now" into "open, then prove it is the one I recorded".
    # 2. Retain the home handle, and do even the existence probe through it. A path-based
    #    `is_dir()` run before the pin just re-opens the same window one step earlier.
    try:
        expected = os.stat(hermes_home)
    except OSError:
        return None  # no anchor: callers must stop, not fall back to path-based deletion
    try:
        home_fd = os.open(hermes_home, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None  # the home vanished under us; nothing is anchored, so nothing is deleted
    try:
        if not os.path.samestat(os.fstat(home_fd), expected):
            os.close(home_fd)
            home_fd = -1
            raise OSError(
                f"{hermes_home} changed identity while anchoring; refusing to delete through it"
            )
        # Relative + O_NOFOLLOW is the containment proof: the kernel resolves "bin" inside the
        # home we hold and refuses to follow it out. An escaping symlinked bin fails right here
        # (ELOOP/ENOTDIR) instead of being detected by a check a race could precede.
        bin_fd = os.open("bin", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
    except OSError as e:
        if home_fd != -1:
            os.close(home_fd)
        if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EINVAL):
            return None  # no bin (or it is a symlinked escape): callers must stop
        raise
    # Belt-and-braces: confirm the pinned bin is still the pinned home's own bin, by identity
    # of the two handles rather than by string comparison of two path lookups.
    if not _is_home_bin(home_fd, bin_fd):
        os.close(bin_fd)
        os.close(home_fd)
        raise OSError(f"{bin_dir} resolves outside the Hermes home; refusing to delete through it")
    return _BinAnchor(fd=bin_fd, home_fd=home_fd)


def _is_home_bin(home_fd: int, bin_fd: int) -> bool:
    """True when ``bin_fd`` is the ``bin`` directory of the home held by ``home_fd``.

    Resolved from the retained handles (``/proc/self/fd``) so the answer describes the
    directories that were actually acquired, not whatever the paths name now. Falls back to
    ``stat`` identity on kernels without ``/proc``; ``bin_fd`` is already ``O_NOFOLLOW``
    relative to ``home_fd``, so this is confirmation, not the primary guard.
    """
    try:
        return os.path.samestat(os.fstat(bin_fd), os.stat("bin", dir_fd=home_fd))
    except OSError:
        return False


def _is_unlinkable_leaf(dir_fd: int, name: str) -> bool:
    """True when ``name`` is a file (or symlink to one) inside the dir held by ``dir_fd``.

    ``follow_symlinks=False`` so the verdict describes the leaf itself, resolved inside the
    anchored bin rather than by re-walking ``<home>/bin``. A symlinked leaf is still a
    removable leftover — only the *link* goes, its target survives — but a directory is
    never a legacy binary this cleanup may delete.
    """
    import stat as _stat

    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except OSError:
        return False
    return _stat.S_ISREG(mode) or _stat.S_ISLNK(mode)


def remove_legacy_managed_uv(hermes_home: Path) -> list[Path]:
    """Delete the pre-PM ``uv``/``uvx`` binaries sitting in ``$HERMES_HOME/bin``.

    Only the binaries go — the directory holds the ``hermes`` launchers and may
    hold the user's own scripts. When ``bin`` is itself a link out of the home,
    or absent, the call refuses instead of deleting through a path
    (:func:`_open_home_bin`). It is best-effort and returns what was removed.
    """
    removed: list[Path] = []
    try:
        with _pm_install_lock():
            anchor = _open_home_bin(hermes_home)
            if anchor is None:
                return []  # The parent was absent; do not race a newly-created link.
            try:
                for uv_name in LEGACY_MANAGED_UV_NAMES:
                    uv_binary = hermes_home / "bin" / uv_name
                    # Inspect the leaf through the SAME anchored chain that deletes it.
                    # A path-based is_file() re-resolves <home>/bin per name, so a swap
                    # could pass the check against the intended bin and unlink elsewhere.
                    if anchor.fd is None or not _is_unlinkable_leaf(anchor.fd, uv_name):
                        continue
                    try:
                        os.unlink(uv_name, dir_fd=anchor.fd)
                        removed.append(uv_binary)
                    except Exception as e:
                        _log_warn(f"Could not remove {uv_binary}: {e}")
            finally:
                if anchor.fd is not None:
                    os.close(anchor.fd)
                if anchor.home_fd is not None:
                    os.close(anchor.home_fd)
    except OSError as e:
        # Fail closed either way: ``TimeoutError`` (an ``OSError`` subclass) means a PM
        # operation may still be writing, and a payload store that is read-only cannot even
        # create the lock file. Leave the binaries for the next run — doctor and `hermes
        # update` both retry — and never let it escape: doctor's check runs with
        # ``on_error=None``, so an exception here would abort the whole run.
        _log_warn(f"Skipped legacy uv cleanup: {e}")
        return []
    return removed
