"""Device Registration Repository 实现。

D2: find_online_devices_by_user 直接表达广播场景的查询意图。
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from pandapal.storage.models import DeviceRegistration
from pandapal.storage.repositories._sqlite_base import BaseRepository


class DeviceRepository(BaseRepository):
    """设备注册信息持久化操作。"""

    def __init__(self, conn: aiosqlite.Connection, timeout: float = 5.0) -> None:
        super().__init__(conn, timeout)

    async def save_device_registration(self, device: DeviceRegistration) -> None:
        """保存设备注册（UPSERT by device_id，幂等）。"""
        registered_at = self._to_iso(device.registered_at) or self._now_iso()
        last_seen = self._to_iso(device.last_seen) or self._now_iso()

        await self._execute(
            "INSERT OR REPLACE INTO device_registrations "
            "(device_id, user_id, channel_type, is_online, registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                device.device_id,
                device.user_id,
                device.channel_type,
                1 if device.is_online else 0,
                registered_at,
                last_seen,
            ),
            operation="save_device_registration",
        )
        await self._commit()

    async def find_device(self, device_id: str) -> DeviceRegistration | None:
        """按 device_id 查找设备。"""
        row = await self._fetchone(
            "SELECT device_id, user_id, channel_type, is_online, registered_at, last_seen "
            "FROM device_registrations WHERE device_id = ?",
            (device_id,),
            operation="find_device",
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def find_online_devices_by_user(
        self, user_id: str
    ) -> list[DeviceRegistration]:
        """查找用户所有在线设备（Broadcast 使用场景）。"""
        rows = await self._fetchall(
            "SELECT device_id, user_id, channel_type, is_online, registered_at, last_seen "
            "FROM device_registrations WHERE user_id = ? AND is_online = 1",
            (user_id,),
            operation="find_online_devices_by_user",
        )
        return [self._row_to_model(row) for row in rows]

    async def update_device_status(
        self, device_id: str, is_online: bool, last_seen: datetime
    ) -> None:
        """更新设备在线状态（高频操作）。"""
        await self._execute(
            "UPDATE device_registrations SET is_online = ?, last_seen = ? "
            "WHERE device_id = ?",
            (1 if is_online else 0, last_seen.isoformat(), device_id),
            operation="update_device_status",
        )
        await self._commit()

    async def delete_device_registration(self, device_id: str) -> None:
        """删除设备注册（幂等）。"""
        await self._execute(
            "DELETE FROM device_registrations WHERE device_id = ?",
            (device_id,),
            operation="delete_device_registration",
        )
        await self._commit()

    @staticmethod
    def _row_to_model(row: tuple) -> DeviceRegistration:
        return DeviceRegistration(
            device_id=row[0],
            user_id=row[1],
            channel_type=row[2],
            is_online=bool(row[3]),
            registered_at=BaseRepository._from_iso(row[4]),
            last_seen=BaseRepository._from_iso(row[5]),
        )
