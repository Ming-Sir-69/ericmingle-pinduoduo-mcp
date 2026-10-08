"""Notification lifecycle uses this service's exact adapter, never another browser."""
from types import SimpleNamespace

import pytest
from test_local_adapter import FakeBridge, adapter_module


def test_notification_refresh_is_same_adapter_and_disabled_by_default(tmp_path):
    module = adapter_module()
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=FakeBridge())
    assert hasattr(api, "notifications")
    assert api.notifications.store is api.watchlist
    assert api.notifications.refresh.__self__ is api
    assert api.notifications.status()["data"]["enabled"] is False


@pytest.mark.asyncio
async def test_shutdown_closes_worker_before_bridge(tmp_path):
    module = adapter_module()
    events = []
    class Bridge(FakeBridge):
        async def aclose(self):
            events.append("bridge.close")
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=Bridge())
    async def close():
        events.append("notification.close")
    api.notifications = SimpleNamespace(close=close)
    await api.shutdown()
    assert events == ["notification.close", "bridge.close"]


@pytest.mark.asyncio
async def test_stdio_starts_worker_and_always_stops_on_transport_failure(tmp_path, monkeypatch):
    module = adapter_module()
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=FakeBridge())
    assert hasattr(api, "notifications")
    monkeypatch.setattr(module, "adapter", api)
    async def run():
        assert api.notifications.task is not None and not api.notifications.task.done()
        raise RuntimeError("fixture transport stopped")
    monkeypatch.setattr(module.mcp, "run_stdio_async", run)
    with pytest.raises(RuntimeError, match="fixture transport stopped"):
        await module.run_server(stdio=True)
    assert api.notifications.task.done() and api.bridge.closed


def test_http_lifespan_starts_and_stops_worker_without_site_reads(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    module = adapter_module()
    api = module.LocalPinduoduoAdapter(tmp_path, bridge=FakeBridge())
    assert hasattr(api, "notifications")
    monkeypatch.setattr(module, "adapter", api)
    monkeypatch.setattr(module, "mcp", module.FastMCP("pdd-lifecycle-fixture"))
    with TestClient(module.create_http_app()):
        assert api.notifications.task is not None and not api.notifications.task.done()
    assert api.notifications.task.done() and api.bridge.closed
    assert not api.bridge.calls
