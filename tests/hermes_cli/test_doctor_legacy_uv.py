"""A pre-PM ``$HERMES_HOME/bin/uv*`` shadows the user's own uv and must be reported and cleared.

PM keeps its uv in the store and deliberately off PATH, so the only ``uv`` a
Hermes install should ever contribute to a shell is none. On Windows the
launcher prepends ``$HERMES_HOME/bin`` to the User PATH and the agent terminal
appends it everywhere else (#101269), so a leftover ``uv`` there is the one
every invocation resolves — and it is a version nothing maintains.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from hermes_cli import doctor, doctor_state


def _uv(path: Path, body: str = "hermes") -> None:
    path.write_text(f"#!/usr/bin/env sh\n# {body}\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HERMES_HOME with the ``bin/`` dir a launcher install publishes."""
    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "bin").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(doctor, "HERMES_HOME", hermes_home)
    return hermes_home


def test_doctor_reports_a_pre_pm_uv_shadow(home, capsys):
    _uv(home / "bin" / "uvx")
    (home / "bin" / "hermes").write_text("#!/usr/bin/env sh\n", encoding="utf-8")

    finding = doctor_state._check_legacy_uv_shadow(False)

    assert any("hermes doctor --fix" in issue for issue in finding.issues)
    assert "shadow your own uv" in capsys.readouterr().out
    assert finding.fixed == 0
    # A report must not delete: the user gets to choose how to clear it.
    assert (home / "bin" / "uvx").is_file()


def test_doctor_fix_removes_only_the_uv_family(home, monkeypatch):
    import pm

    monkeypatch.setattr(pm, "is_installed", lambda name: True)
    for name in ("uv", "uvx", "uv.exe", "uvx.exe"):
        _uv(home / "bin" / name)
    (home / "bin" / "hermes").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    (home / "bin" / "mytool").write_text("#!/usr/bin/env sh\n", encoding="utf-8")

    finding = doctor_state._check_legacy_uv_shadow(True)

    assert finding.fixed == 4
    assert not finding.issues
    for name in ("uv", "uvx", "uv.exe", "uvx.exe"):
        assert not (home / "bin" / name).exists()
    # ``bin/`` holds the launchers and the user's own scripts — never those.
    assert (home / "bin" / "hermes").is_file()
    assert (home / "bin" / "mytool").is_file()


def test_doctor_fix_keeps_the_family_until_the_store_has_uv(home, monkeypatch, capsys):
    """``--fix`` must not trade the install's only uv for none: while PM's store has no uv the
    legacy binary is still what this install runs on (same guard as
    ``update_cmd_maint._purge_legacy_managed_uv``: remove only once a private
    target exists). The shadowing is still reported, so the run exits non-zero with a next step."""
    import pm

    monkeypatch.setattr(pm, "is_installed", lambda name: False)
    _uv(home / "bin" / "uv")
    (home / "bin" / "hermes").write_text("#!/usr/bin/env sh\n", encoding="utf-8")

    finding = doctor_state._check_legacy_uv_shadow(True)

    assert finding.fixed == 0
    assert finding.issues
    assert (home / "bin" / "uv").is_file()
    out = capsys.readouterr().out
    assert "shadow your own uv" in out
    assert "hermes update" in finding.issues[0]


def test_doctor_is_quiet_once_the_family_is_gone(home):
    (home / "bin" / "hermes").write_text("#!/usr/bin/env sh\n", encoding="utf-8")

    finding = doctor_state._check_legacy_uv_shadow(True)

    assert not finding.issues
    assert finding.fixed == 0


def test_legacy_bin_uv_shadows_the_users_uv_until_the_cleanup(home, tmp_path):
    """End-to-end: on a launcher layout the leftover wins, after cleanup the user's does."""
    from hermes_cli.legacy_uv import remove_legacy_managed_uv
    from tools.environments.local import _append_missing_sane_path_entries, _managed_runtime_path_entries

    user_bin = tmp_path / "user-bin"
    user_bin.mkdir()
    _uv(home / "bin" / "uv", body="stale-hermes-uv")
    _uv(user_bin / "uv", body="users-own-uv")

    # The agent terminal's PATH carries $HERMES_HOME/bin for the launchers.
    assert str(home / "bin") in _managed_runtime_path_entries()

    # Windows-style launcher layout: $HERMES_HOME/bin is ahead of the user's PATH.
    def composed() -> str:
        return _append_missing_sane_path_entries(os.pathsep.join([str(home / "bin"), str(user_bin)]))

    assert shutil.which("uv", path=composed()) == str(home / "bin" / "uv")

    assert [p.name for p in remove_legacy_managed_uv(home)] == ["uv"]

    assert shutil.which("uv", path=composed()) == str(user_bin / "uv")
    assert str(home / "bin") in composed().split(os.pathsep)


@pytest.mark.platforms("posix")
def test_remove_legacy_uv_refuses_a_linked_bin(tmp_path, monkeypatch):
    """``$HERMES_HOME/bin`` symlinked into an ordinary user bin: ``is_file()``/``unlink()``
    follow that PARENT, so the four-name allowlist would delete the USER's real files — the
    opposite of this helper's whole guarantee. Refuse before the first unlink; the link and
    the external files all stay."""
    from hermes_cli.legacy_uv import LEGACY_MANAGED_UV_NAMES, remove_legacy_managed_uv

    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    home = tmp_path / "hermes"
    outside = tmp_path / "user-bin"
    home.mkdir()
    outside.mkdir()
    for name in LEGACY_MANAGED_UV_NAMES:
        (outside / name).write_text("user-owned; preserve", encoding="utf-8")
    (home / "bin").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert remove_legacy_managed_uv(home) == []
    assert (home / "bin").is_symlink()
    for name in LEGACY_MANAGED_UV_NAMES:
        assert (outside / name).read_text(encoding="utf-8") == "user-owned; preserve"


@pytest.mark.platforms("posix")
def test_missing_bin_never_authorizes_a_later_path_unlink(tmp_path, monkeypatch):
    """No directory anchor at probe time means no deletion at all.

    The probe and the leaf work are not atomic: a concurrent writer can publish
    ``bin -> user-bin`` after ``_open_home_bin`` found no ``bin`` and before the
    first ``is_file()``. Both that lookup and the unlink then traverse the new
    parent, past the containment check and the ``O_NOFOLLOW`` fd. The callback
    below calls the REAL acquisition and mutates the tree only afterwards, so
    the ordering — not a stubbed decision — is what the assertion exercises.
    """
    from hermes_cli import legacy_uv
    from hermes_cli.uninstall import remove_legacy_runtime_trees

    home, outside, store = (tmp_path / name for name in ("home", "outside", "store"))
    for directory in (home, outside, store):
        directory.mkdir()
    for name in legacy_uv.LEGACY_MANAGED_UV_NAMES:
        (outside / name).write_text("USER-OWNED", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))
    original = legacy_uv._open_home_bin

    def publish_after_probe(hermes_home):
        anchor = original(hermes_home)
        assert anchor is None, "the probe must find no bin/ before the link is published"
        (hermes_home / "bin").symlink_to(outside, target_is_directory=True)
        return anchor

    monkeypatch.setattr(legacy_uv, "_open_home_bin", publish_after_probe)

    remove_legacy_runtime_trees(home)

    for name in legacy_uv.LEGACY_MANAGED_UV_NAMES:
        assert (outside / name).read_text(encoding="utf-8") == "USER-OWNED", name


@pytest.mark.platforms("posix")
def test_doctor_does_not_name_a_linked_bin_as_removable(tmp_path, monkeypatch, capsys):
    """``$HERMES_HOME/bin`` symlinked into an ordinary user bin: doctor must not report the
    user's own uv behind that link as a pre-PM leftover to remove, and ``--fix`` must not
    touch it. The deletion side already refuses (the uninstall twin above); this asserts the
    REPORT side no longer contradicts it by telling the user to delete their own files."""
    from hermes_cli.legacy_uv import LEGACY_MANAGED_UV_NAMES

    home = tmp_path / "hermes"
    outside = tmp_path / "user-bin"
    home.mkdir()
    outside.mkdir()
    for name in LEGACY_MANAGED_UV_NAMES:
        _uv(outside / name, body="users-own-uv")
    (home / "bin").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(doctor, "HERMES_HOME", home)

    finding = doctor_state._check_legacy_uv_shadow(True)

    assert not finding.issues, "the user's own uv is not a removable finding"
    assert finding.fixed == 0
    for name in LEGACY_MANAGED_UV_NAMES:
        assert "users-own-uv" in (outside / name).read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "outside the Hermes home" in out
    assert "Remove" not in out


@pytest.mark.platforms("posix")
def test_remove_legacy_uv_unlinks_only_the_leaf_link(tmp_path, monkeypatch):
    """The refusal is about the PARENT dir link, not a leaf: with an ordinary ``bin`` dir the
    leftover ``bin/uv`` symlink itself may be removed — its external target survives — and
    unrelated scripts in the dir are never candidates."""
    from hermes_cli.legacy_uv import remove_legacy_managed_uv

    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    home = tmp_path / "hermes"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    user_uv = tmp_path / "user-bin" / "uv"
    user_uv.parent.mkdir()
    user_uv.write_text("users-own-uv", encoding="utf-8")
    (bin_dir / "uv").symlink_to(user_uv)
    (bin_dir / "hermes").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    removed = remove_legacy_managed_uv(home)

    assert removed == [bin_dir / "uv"]
    assert not (bin_dir / "uv").is_symlink()
    assert user_uv.read_text(encoding="utf-8") == "users-own-uv"
    assert (bin_dir / "hermes").is_file()


@pytest.mark.platforms("posix")
def test_remove_legacy_uv_accepts_a_symlinked_home(tmp_path, monkeypatch):
    """A symlinked *home* is anchored, not an escape.

    ``realpath(bin)`` equals ``realpath(home)/bin``, so the cleanup still runs; the containment
    check must stay realpath identity rather than degrade into a string-prefix comparison that
    would refuse a home reached through a link."""
    from hermes_cli.legacy_uv import remove_legacy_managed_uv

    real_home = tmp_path / "real-home"
    (real_home / "bin").mkdir(parents=True)
    legacy = real_home / "bin" / "uv"
    legacy.write_text("legacy", encoding="utf-8")
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(linked_home))

    assert remove_legacy_managed_uv(linked_home) == [linked_home / "bin" / "uv"]
    assert not legacy.exists()


@pytest.mark.platforms("posix")
def test_remove_legacy_uv_stays_anchored_after_a_link_swap(tmp_path, monkeypatch):
    """After a successful acquisition, replacing ``bin`` with a link out of the home cannot
    redirect the unlinks: they run through the directory fd pinned at open time, so the
    external file behind the newly published link is never the target."""
    from hermes_cli import legacy_uv

    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "uv").write_text("legacy", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "uv"
    sentinel.write_text("USER-OWNED", encoding="utf-8")
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    original = legacy_uv._open_home_bin

    def swap_after_acquire(hermes_home):
        anchor = original(hermes_home)
        assert anchor is not None and anchor.fd is not None
        (hermes_home / "bin").rename(hermes_home / "bin.orig")
        (hermes_home / "bin").symlink_to(outside, target_is_directory=True)
        return anchor

    monkeypatch.setattr(legacy_uv, "_open_home_bin", swap_after_acquire)

    assert legacy_uv.remove_legacy_managed_uv(home) == [home / "bin" / "uv"]
    assert not (home / "bin.orig" / "uv").exists()
    assert sentinel.read_text(encoding="utf-8") == "USER-OWNED"


@pytest.mark.platforms("posix")
def test_remove_legacy_uv_refuses_home_identity_swap_before_bin_open(tmp_path, monkeypatch):
    """A swap between the home identity observation and child acquisition must fail closed.

    This is the narrower KEE-24 race: ``home`` is renamed aside and replaced with a
    symlink to an external tree before ``bin`` is opened. The final component is still
    an ordinary directory, so a path-based ``open(home/bin, O_NOFOLLOW)`` would pin
    and delete from the wrong directory.
    """
    from hermes_cli import legacy_uv

    home = tmp_path / "home"
    external = tmp_path / "external"
    (home / "bin").mkdir(parents=True)
    (external / "bin").mkdir(parents=True)
    for name in legacy_uv.LEGACY_MANAGED_UV_NAMES:
        (home / "bin" / name).write_text("legacy", encoding="utf-8")
        (external / "bin" / name).write_text("USER-OWNED", encoding="utf-8")

    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from contextlib import nullcontext

    monkeypatch.setattr(legacy_uv, "_pm_install_lock", lambda: nullcontext())
    real_stat = legacy_uv.os.stat
    fired = False

    def swap_after_home_stat(path, *args, **kwargs):
        nonlocal fired
        result = real_stat(path, *args, **kwargs)
        if not fired and Path(path) == home and not kwargs.get("dir_fd"):
            fired = True
            home.rename(tmp_path / "saved-home")
            home.symlink_to(external, target_is_directory=True)
        return result

    monkeypatch.setattr(legacy_uv.os, "stat", swap_after_home_stat)

    assert legacy_uv.remove_legacy_managed_uv(home) == []
    assert fired
    for name in legacy_uv.LEGACY_MANAGED_UV_NAMES:
        assert (external / "bin" / name).read_text(encoding="utf-8") == "USER-OWNED", name
        assert (tmp_path / "saved-home" / "bin" / name).read_text(encoding="utf-8") == "legacy", name


@pytest.mark.platforms("windows")
def test_remove_legacy_uv_refuses_a_junctioned_bin(tmp_path, monkeypatch):
    """Native Windows twin of the linked-bin refusal: a junction into the user's bin would
    delete the external directory's real files. ``mklink /J`` needs no elevation, so the
    guard is provable on a real Windows host (POSIX side above)."""
    import subprocess

    from hermes_cli.legacy_uv import LEGACY_MANAGED_UV_NAMES, remove_legacy_managed_uv

    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    home = tmp_path / "hermes"
    outside = tmp_path / "user-bin"
    home.mkdir()
    outside.mkdir()
    for name in LEGACY_MANAGED_UV_NAMES:
        (outside / name).write_text("user-owned; preserve", encoding="utf-8")
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(home / "bin"), str(outside)],
        capture_output=True,
        text=True,
    )
    assert linked.returncode == 0, linked.stderr
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert remove_legacy_managed_uv(home) == []
    for name in LEGACY_MANAGED_UV_NAMES:
        assert (outside / name).read_text(encoding="utf-8") == "user-owned; preserve"


def test_doctor_reports_partial_cleanup_as_unresolved(home, monkeypatch):
    """One name failing mid-family: the three that went count as fixes, the one that stayed
    remains an unresolved finding — a clean result over a live leftover is the bug."""
    import pm

    real_unlink = os.unlink

    def _fail_one(name, *, dir_fd=None):
        if os.path.basename(str(name)) == "uvx.exe":
            raise PermissionError("simulated unlink failure")
        return real_unlink(name, dir_fd=dir_fd) if dir_fd is not None else real_unlink(name)

    monkeypatch.setattr(pm, "is_installed", lambda name: True)
    monkeypatch.setattr("hermes_cli.legacy_uv.os.unlink", _fail_one)
    for name in ("uv", "uvx", "uv.exe", "uvx.exe"):
        _uv(home / "bin" / name)

    finding = doctor_state._check_legacy_uv_shadow(True)

    assert finding.fixed == 3
    assert finding.issues, "the name that stayed is an unresolved finding"
    assert (home / "bin" / "uvx.exe").is_file()
    for name in ("uv", "uvx", "uv.exe"):
        assert not (home / "bin" / name).exists()
