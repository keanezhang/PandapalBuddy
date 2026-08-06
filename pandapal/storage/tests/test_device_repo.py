"""DeviceRepository 测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pandapal.storage.models import DeviceRegistration


@pytest.mark.asyncio
async def test_save_and_find(memory_storage):
    """保存并查找设备注册。"""
    repo = memory_storage.get_device_repo()
    now = datetime.now(timezone.utc)
    device = DeviceRegistration(
        device_id="d1",
        user_id="u1",
        channel_type="wecom",
        is_online=True,
        registered_at=now,
        last_seen=now,
    )
    await repo.save_device_registration(device)
    found = await repo.find_device("d1")

    assert found is not None
    assert found.device_id == "d1"
    assert found.channel_type == "wecom"
    assert found.is_online is True


@pytest.mark.asyncio
async def test_find_online_devices_by_user(memory_storage):
    """查找用户所有在线设备。"""
    repo = memory_storage.get_device_repo()
    now = datetime.now(timezone.utc)

    await repo.save_device_registration(DeviceRegistration(
        device_id="d1", user_id="u1", channel_type="wecom",
        is_online=True, registered_at=now, last_seen=now,
    ))
    await repo.save_device_registration(DeviceRegistration(
        device_id="d2", user_id="u1", channel_type="cli",
        is_online=False, registered_at=now, last_seen=now,
    ))
    await repo.save_device_registration(DeviceRegistration(
        device_id="d3", user_id="u1", channel_type="mobile",
        is_online=True, registered_at=now, last_seen=now,
    ))

    online = await repo.find_online_devices_by_user("u1")
    assert len(online) == 2
    ids = {d.device_id for d in online}
    assert "d1" in ids and "d3" in ids


@pytest.mark.asyncio
async def test_update_device_status(memory_storage):
    """更新设备在线状态。"""
    repo = memory_storage.get_device_repo()
    now = datetime.now(timezone.utc)

    await repo.save_device_registration(DeviceRegistration(
        device_id="d1", user_id="u1", channel_type="wecom",
        is_online=True, registered_at=now, last_seen=now,
    ))

    await repo.update_device_status("d1", False, now)
    found = await repo.find_device("d1")
    assert found is not None
    assert found.is_online is False


@pytest.mark.asyncio
async def test_delete_device(memory_storage):
    """删除设备注册（幂等）。"""
    repo = memory_storage.get_device_repo()
    now = datetime.now(timezone.utc)

    await repo.save_device_registration(DeviceRegistration(
        device_id="d1", user_id="u1", channel_type="wecom",
        is_online=True, registered_at=now, last_seen=now,
    ))

    await repo.delete_device_registration("d1")
    assert await repo.find_device("d1") is None

    # 幂等 — 再次删除不报错
    await repo.delete_device_registration("d1")
