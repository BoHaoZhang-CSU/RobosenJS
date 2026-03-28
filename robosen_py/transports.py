from __future__ import annotations

from collections.abc import Awaitable, Callable

PacketCallback = Callable[[bytes], Awaitable[None] | None]


class BleTransport:
    """BLE transport based on bleak, matching RobosenJS connection behavior."""

    def __init__(self, service_uuid: str, characteristic_uuid: str, name: str, code: str) -> None:
        self.service_uuid = service_uuid
        self.characteristic_uuid = characteristic_uuid
        self.name = name
        self.code = code
        self.client = None

    async def connect(self, callback: PacketCallback) -> str:
        from bleak import BleakClient, BleakScanner

        device = await BleakScanner.find_device_by_filter(
            lambda d, _: d.name == self.name or (d.name and self.code in d.name)
        )
        if device is None:
            raise RuntimeError(f"Robot not found: {self.name} / {self.code}")

        self.client = BleakClient(device)
        await self.client.connect()

        async def _notify(_: int, data: bytearray) -> None:
            result = callback(bytes(data))
            if result is not None:
                await result

        await self.client.start_notify(self.characteristic_uuid, _notify)
        return device.name or self.name

    async def write(self, payload: bytes) -> None:
        if self.client is None:
            raise RuntimeError("Transport not connected")
        await self.client.write_gatt_char(self.characteristic_uuid, payload, response=False)

    async def disconnect(self) -> None:
        if self.client is None:
            return
        await self.client.stop_notify(self.characteristic_uuid)
        await self.client.disconnect()
        self.client = None


class MockTransport:
    """Test transport used for unit tests and offline scripting."""

    def __init__(self) -> None:
        self.callback: PacketCallback | None = None
        self.sent: list[bytes] = []

    async def connect(self, callback: PacketCallback) -> str:
        self.callback = callback
        return "Mock-K1"

    async def write(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def disconnect(self) -> None:
        self.callback = None

    async def feed(self, payload: bytes) -> None:
        if self.callback is not None:
            maybe = self.callback(payload)
            if maybe is not None:
                await maybe
