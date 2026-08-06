"""Markdown Device Repository 实现（异步接口）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pandapal.storage.models import DeviceRegistration
from pandapal.storage.repositories._markdown_base import MarkdownBaseRepository


class MarkdownDeviceRepository(MarkdownBaseRepository):
    """Markdown 设备注册持久化操作（异步接口）。"""

    def __init__(self, base_dir: str, timeout: float = 5.0) -> None:
        super().__init__(base_dir, "devices", timeout)

    # ──────────────────────────────────────────────
    # CRUD 操作（异步接口）
    # ──────────────────────────────────────────────

    async def save_device(self, device: DeviceRegistration) -> None:
        """保存设备注册信息。"""
        file_path = self._get_file_path(device.device_id)
        data = {
            "device_id": device.device_id,
            "user_id": device.user_id,
            "device_name": device.device_name,
            "platform": device.platform,
            "last_seen": self._to_iso(device.last_seen),
            "is_online": device.is_online,
            "created_at": self._to_iso(device.created_at) or self._now_iso(),
        }
        title = f"Device: {device.device_name}"
        await self._write_entity(file_path, data, title)

    async def find_device(self, device_id: str) -> DeviceRegistration | None:
        """按 device_id 查找设备。"""
        file_path = self._get_file_path(device_id)
        data = await self._read_entity(file_path)
        return self._dict_to_model(data) if data else None

    async def find_devices_by_user(self, user_id: str) -> list[DeviceRegistration]:
        """按 user_id 查找所有设备。"""
        entities = await self._filter_entities(user_id=user_id)
        return [self._dict_to_model(data) for data in entities if data]

    async def find_online_devices_by_user(self, user_id: str) -> list[DeviceRegistration]:
        """查找用户的所有在线设备。"""
        entities = await self._filter_entities(user_id=user_id, is_online=True)
        return [self._dict_to_model(data) for data in entities if data]

    async def update_device_heartbeat(self, device_id: str, timestamp: Any) -> None:
        """更新设备心跳时间。"""
        file_path = self._get_file_path(device_id)
        data = await self._read_entity(file_path)
        if data:
            data["last_seen"] = self._to_iso(timestamp)
            data["is_online"] = True
            await self._write_entity(file_path, data, f"Device: {data.get('device_name', '')}")

    async def set_device_offline(self, device_id: str) -> None:
        """将设备标记为离线。"""
        file_path = self._get_file_path(device_id)
        data = await self._read_entity(file_path)
        if data:
            data["is_online"] = False
            await self._write_entity(file_path, data, f"Device: {data.get('device_name', '')}")

    async def delete_device(self, device_id: str) -> None:
        """删除设备注册信息。"""
        file_path = self._get_file_path(device_id)
        await self._delete_entity(file_path)

    # ──────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _dict_to_model(data: dict[str, Any]) -> DeviceRegistration:
        """将字典转换为 DeviceRegistration 模型。"""

        def parse_datetime(value):
            if not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return None

        return DeviceRegistration(
            device_id=data.get("device_id", ""),
            user_id=data.get("user_id", ""),
            device_name=data.get("device_name", ""),
            platform=data.get("platform", "unknown"),
            last_seen=parse_datetime(data.get("last_seen")),
            is_online=data.get("is_online", False),
            created_at=parse_datetime(data.get("created_at")),
        )
