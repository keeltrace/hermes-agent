import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "setup-hermes.sh"


def test_setup_hermes_script_is_valid_shell():
    result = subprocess.run(["bash", "-n", str(SETUP_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "home_kind",
    [
        "custom",
        "profile",
        "profile_trailing_slash",
        "custom_profile_root",
        "custom_profiles_segment",
        "profiles_ancestor_profile",
        "runtime_override",
    ],
)
def test_setup_stages_uv_into_pms_store_root(tmp_path, monkeypatch, home_kind):
    """setup-hermes.sh stages uv where pm.paths.store_root() resolves it (#101269).

    The script hardcoded ~/.hermes/tools while pm's own store follows
    HERMES_HOME, so with HERMES_HOME set the two never met: the script staged a
    sha256-verified uv PM could not see, and PM re-downloaded it.
    """
    bash = shutil.which("bash")
    assert bash, "setup-hermes.sh is a bash script"

    # Deliberately different from $HOME/.hermes, so a hardcoded default in the
    # script stages somewhere pm will never look.
    home = tmp_path / "custom-home" / ".hermes"
    if home_kind == "custom":
        hermes_home = home
    elif home_kind in {"profile", "profile_trailing_slash"}:
        hermes_home = home / "profiles" / "coder"
    elif home_kind == "custom_profile_root":
        hermes_home = tmp_path / "custom-root" / "profiles" / "coder"
    elif home_kind == "profiles_ancestor_profile":
        # A named profile under a custom home that itself has an unrelated ``profiles``
        # ancestor: only the immediate pair may fold, not the ancestor segment.
        hermes_home = tmp_path / "profiles" / "alice" / ".hermes" / "profiles" / "coder"
    else:
        hermes_home = tmp_path / "profiles" / "alice" / ".hermes"
    monkeypatch.setenv("HOME", str(tmp_path / "native-home"))
    hermes_home_env = str(hermes_home)
    if home_kind == "profile_trailing_slash":
        hermes_home_env += os.sep
    monkeypatch.setenv("HERMES_HOME", hermes_home_env)
    if home_kind == "runtime_override":
        runtime_store = tmp_path / "runtime-store"
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(runtime_store))
    else:
        runtime_store = None
        monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)

    from pm.paths import store_root

    store = Path(store_root())
    if runtime_store is not None:
        expected_store = runtime_store
    elif home_kind in {"profile", "profile_trailing_slash"}:
        expected_store = home / "tools"
    elif home_kind in {"custom_profile_root", "profiles_ancestor_profile"}:
        expected_store = hermes_home.parent.parent / "tools"
    else:
        expected_store = hermes_home / "tools"
    assert store == expected_store, "pm must resolve its store under the machine-scoped Hermes root"

    lock = json.loads((REPO_ROOT / "pm" / "lock.json").read_text(encoding="utf-8"))
    uv_version = lock["packages"]["uv"]["version"]
    system = "darwin" if sys.platform == "darwin" else "linux"
    arch = "arm64" if platform.machine() in {"arm64", "aarch64"} else "x64"
    uv = store / f"uv-{uv_version}-{system}-{arch}" / "uv"
    uv.parent.mkdir(parents=True)
    record = tmp_path / f"uv-state-{home_kind}"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  --version) echo "uv 0.0.0-test"; exit 0 ;;\n'
        f'  python) printf "%s|%s|%s\\n" "$0" "$UV_CACHE_DIR" '
        f'"$UV_PYTHON_INSTALL_DIR" >> {shlex.quote(str(record))}; exit 1 ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)

    # A staging attempt must fail locally instead of reaching the network.
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    curl = stub_dir / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 6\n", encoding="utf-8")
    curl.chmod(0o755)

    env = {**os.environ, "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        [bash, str(SETUP_SCRIPT)], env=env, cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr
    assert "Staging pinned uv" not in output, output
    assert "pinned uv found" in output, output
    assert record.is_file(), "setup-hermes.sh never executed the pinned store uv"
    expected_root = (
        hermes_home.parent.parent
        if home_kind in {"profile", "profile_trailing_slash", "custom_profile_root",
                         "profiles_ancestor_profile"}
        else hermes_home
    )
    for line in record.read_text(encoding="utf-8").splitlines():
        invoked_uv, cache_dir, python_dir = line.split("|")
        assert Path(invoked_uv) == uv, line
        assert cache_dir == str(expected_root / "cache" / "uv"), line
        assert python_dir == str(expected_root / "cache" / "uv-python"), line
