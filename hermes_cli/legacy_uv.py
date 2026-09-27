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
    cannot redirect them. Windows has no ``dir_fd``: there the verified directory
    is recorded as ``fd=None`` and its leaf unlinks go by path (the realpath
    containment check is that platform's whole guard). An *absent* ``bin`` is not
    representable — :func:`_open_home_bin` returns ``None`` instead, because
    "nothing was acquired" means "delete nothing".
    """

    fd: int | None


def _open_home_bin(hermes_home: Path) -> _BinAnchor | None:
    """Anchor ``<home>/bin`` for the deletions below; ``None`` when there is no directory.

    Refuses (raises ``OSError``) before the first unlink when ``bin`` escapes the home
    (:func:`bin_escapes_home`). On POSIX the directory is additionally opened ``O_NOFOLLOW``
    and the handle kept, so the unlinks act on THIS directory rather than on a path a race
    could re-point; Windows has no ``dir_fd``, so its realpath check (junctions resolve under
    it) is the whole guard — including for the window between this check and an unlink, which
    the returned anchor cannot close on that platform.
    """
    bin_dir = hermes_home / "bin"
    if not bin_dir.is_dir():
        return None  # no anchor: callers must stop, not fall back to path-based deletion
    if bin_escapes_home(hermes_home):
        raise OSError(f"{bin_dir} resolves outside the Hermes home; refusing to delete through it")
    if os.name == "posix":
        return _BinAnchor(fd=os.open(bin_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
    return _BinAnchor(fd=None)  # Windows: verified directory, no dir_fd to pin


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
                    if not uv_binary.is_file():
                        continue
                    try:
                        if anchor.fd is None:
                            uv_binary.unlink()
                        else:
                            os.unlink(uv_name, dir_fd=anchor.fd)
                        removed.append(uv_binary)
                    except Exception as e:
                        _log_warn(f"Could not remove {uv_binary}: {e}")
            finally:
                if anchor.fd is not None:
                    os.close(anchor.fd)
    except OSError as e:
        # Fail closed either way: ``TimeoutError`` (an ``OSError`` subclass) means a PM
        # operation may still be writing, and a payload store that is read-only cannot even
        # create the lock file. Leave the binaries for the next run — doctor and `hermes
        # update` both retry — and never let it escape: doctor's check runs with
        # ``on_error=None``, so an exception here would abort the whole run.
        _log_warn(f"Skipped legacy uv cleanup: {e}")
        return []
    return removed
