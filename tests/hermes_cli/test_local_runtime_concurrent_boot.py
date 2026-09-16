"""Regression coverage for concurrent managed local-runtime boot."""

from __future__ import annotations

import threading
import time


def test_concurrent_boots_spawn_one_router(tmp_path, monkeypatch):
    """Concurrent boot callers must share one process-local supervisor."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import hermes_cli.local_runtime.bootstrap as bs
    import hermes_cli.local_runtime.binaries as bins
    import hermes_cli.local_runtime.endpoint as ep
    import hermes_cli.local_runtime.supervisor as sup_mod

    monkeypatch.setattr(bs, "_SUPERVISOR", None)
    monkeypatch.setattr(bs, "staged_models", lambda: [tmp_path / "m.gguf"])
    monkeypatch.setattr(ep, "_state_endpoint", lambda: None)
    monkeypatch.setattr(bins, "installed_tags", lambda: ["b1"])
    monkeypatch.setattr(bins, "default_tag", lambda: "b1")
    monkeypatch.setattr(bins, "ensure_runtime_installed", lambda tag, backend: tmp_path)
    monkeypatch.setattr(bs, "_generate_presets", lambda mdir, path: None)
    monkeypatch.setattr(bs, "_start_idle_sweeper", lambda sup: None)

    started = []
    in_start = threading.Event()

    class _FakeSupervisor:
        def __init__(self, *args, **kwargs):
            self.base_url = "http://127.0.0.1:1/v1"

        def start(self):
            started.append(self)
            in_start.set()
            time.sleep(0.3)

    monkeypatch.setattr(sup_mod, "LlamaServerSupervisor", _FakeSupervisor)

    cfg = {"local_runtime": {"enabled": True, "backend": "cpu"}}
    results = []
    first = threading.Thread(target=lambda: results.append(bs.ensure_local_runtime(cfg)))
    first.start()
    assert in_start.wait(5)
    second = threading.Thread(target=lambda: results.append(bs.ensure_local_runtime(cfg)))
    second.start()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(started) == 1
    assert results[0] is results[1] is started[0]
