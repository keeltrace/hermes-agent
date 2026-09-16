"""Regression coverage for Kanban identity forwarded to terminal plugins."""

from types import SimpleNamespace

from tools import terminal_tool_backends as backends


def test_plugin_backend_receives_dispatcher_kanban_identity(monkeypatch):
    received = {}

    class Provider:
        name = "testbox"

        @staticmethod
        def create_environment(*, cwd, timeout, task_id, image, container_config, kanban_task_id=None):
            received["kanban_task_id"] = kanban_task_id
            return SimpleNamespace()

    monkeypatch.setattr(backends, "_get_plugin_env_provider", lambda _name: Provider())
    monkeypatch.setenv("HERMES_KANBAN_TASK", "kanban-task-123")

    backends._build_plugin_env(
        env_type="testbox",
        image="python:3.12",
        cwd="/workspace",
        timeout=30,
        cc={},
        task_id="session:test",
    )

    assert received["kanban_task_id"] == "kanban-task-123"


def test_plugin_backend_without_kanban_parameter_still_works(monkeypatch):
    received = {}

    class Provider:
        name = "legacybox"

        @staticmethod
        def create_environment(*, cwd, timeout, task_id, image, container_config):
            received["task_id"] = task_id
            return SimpleNamespace()

    monkeypatch.setattr(backends, "_get_plugin_env_provider", lambda _name: Provider())
    monkeypatch.setenv("HERMES_KANBAN_TASK", "kanban-task-123")

    backends._build_plugin_env(
        env_type="legacybox",
        image="python:3.12",
        cwd="/workspace",
        timeout=30,
        cc={},
        task_id="session:test",
    )

    assert received["task_id"] == "session:test"
