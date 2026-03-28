from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robosen_py.transports import BleTransport


PACKET_NONE = "none"
PACKET_DATA = "data"
PACKET_COMPLETED = "completed"
PACKET_PROGRESS = "progress"

STATUS_INITIAL = 0
STATUS_READY = 1
STATUS_BUSY = 2
STATUS_STOP = 3


@dataclass(slots=True)
class ReceivedPacket:
    kind: str
    type: str
    data: Any
    raw: bytes
    parsed: dict[str, Any]


class Robot:
    def __init__(self, code: str, options: dict[str, Any] | None = None) -> None:
        options = options or {}
        base = Path(__file__).resolve().parent.parent / "src" / code
        config_file = options.get("file", "robot")
        if not str(config_file).endswith(".json"):
            config_file = f"{config_file}.json"
        config_path = base / config_file
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self._merge(self.config, options)
        self.config = options
        self.code = self.config.get("code", code)
        self.name = self.config.get("name", self.code)

        self.status = STATUS_INITIAL
        self._queue: asyncio.Queue[ReceivedPacket] = asyncio.Queue()
        self._llm = None

        self._link_config()

        self.joint_initial = {k: v["value"] for k, v in self.config["joint"].items()}
        self.joint_current = dict(self.joint_initial)
        self.lock_current = {
            j["name"]: 1 for j in self.config["joint"].values() if isinstance(j, dict) and j.get("joint")
        }
        self._transport = options.get("transport") or BleTransport(
            self.config["spec"]["serviceUuid"],
            self.config["spec"]["characteristicsUuid"],
            self.name,
            self.code,
        )

    def _merge(self, src: dict[str, Any], dst: dict[str, Any]) -> None:
        for k, v in src.items():
            if isinstance(v, dict):
                cur = dst.setdefault(k, {})
                if isinstance(cur, dict):
                    self._merge(v, cur)
                else:
                    dst[k] = v
            else:
                dst.setdefault(k, v)

    def _link_config(self) -> None:
        for section, section_cfg in self.config.items():
            if not isinstance(section_cfg, dict):
                continue
            for name, entry in section_cfg.items():
                if isinstance(entry, dict):
                    entry.setdefault("name", name)

        for group_name, group in self.config["command"].items():
            if not isinstance(group, dict) or group.get("group") is not True:
                continue
            for name, cmd in group.items():
                if not isinstance(cmd, dict):
                    continue
                cmd.setdefault("name", name)
                cmd.setdefault("group", group_name)
                cmd.setdefault("kind", PACKET_DATA)
                if group.get("derive"):
                    cmd.setdefault("data", f"{group_name}/{name}")
                for k, v in group.items():
                    if k in {"group", "derive"}:
                        continue
                    if not isinstance(v, dict):
                        cmd.setdefault(k, v)

        for cmd in self.commands().values():
            py_name = self.to_snake_case(cmd["name"])
            if not hasattr(self, py_name):
                setattr(self, py_name, self._mk_dynamic_command(cmd["name"]))

    def _mk_dynamic_command(self, command_name: str):
        async def _run(*args: Any):
            return await self.command(command_name, list(args))

        return _run

    async def on(self) -> None:
        self.name = await self._transport.connect(self._on_notification)
        await self.wait(self.config["duration"].get("announcement", 0))
        self.status = STATUS_READY

    async def off(self) -> None:
        await self._transport.disconnect()
        self.status = STATUS_INITIAL

    async def end(self) -> None:
        await self.stop()
        await self.off()

    def connected(self) -> bool:
        return self.status != STATUS_INITIAL

    def ready(self) -> bool:
        return self.status == STATUS_READY

    def busy(self) -> bool:
        return self.status == STATUS_BUSY

    async def wait(self, milliseconds: int | None) -> None:
        if milliseconds and milliseconds > 0:
            await asyncio.sleep(milliseconds / 1000.0)

    async def _on_notification(self, data: bytes) -> None:
        await self._queue.put(self.parse_packet(data))

    def _type_config(self, type_code: str) -> dict[str, Any] | None:
        return next((t for t in self.config["type"].values() if t.get("code") == type_code), None)

    def checksum(self, payload: bytes) -> int:
        return sum(payload) % 256

    def packet(self, type_code: str, data: Any = "") -> bytes:
        encoded = self._encode(self._type_config(type_code), data)
        header = bytes.fromhex(self.config["spec"]["header"])
        t = bytes.fromhex(type_code)
        if isinstance(encoded, bytes):
            body_data = encoded
        elif isinstance(encoded, bytearray):
            body_data = bytes(encoded)
        elif isinstance(encoded, list):
            body_data = bytes(encoded)
        elif isinstance(encoded, str):
            body_data = encoded.encode("utf-8")
        elif isinstance(encoded, int):
            body_data = bytes([encoded])
        else:
            body_data = b""
        body = bytes([1 + len(body_data) + 1]) + t + body_data
        return header + body + bytes([self.checksum(body)])

    def packet_command(self, command: dict[str, Any] | str | bytes) -> bytes:
        if isinstance(command, bytes):
            return command
        if isinstance(command, str):
            return bytes.fromhex(command)
        data = command.get("data")
        if data is None:
            t = self._type_config(command["type"])
            data = (t or {}).get("data", "")
        return self.packet(command["type"], data)

    def parse_packet(self, payload: bytes) -> ReceivedPacket:
        hex_payload = payload.hex()
        header = self.config["spec"]["header"]
        if not hex_payload.startswith(header) or len(hex_payload) < 10:
            return ReceivedPacket("invalid", "", None, payload, {})
        n = int(hex_payload[4:6], 16)
        body = hex_payload[4 : 4 + n * 2]
        type_code = body[2:4]
        data_hex = body[4:]
        raw_data = bytes.fromhex(data_hex) if data_hex else b""
        type_cfg = self._type_config(type_code) or {}

        value: Any = data_hex
        if len(raw_data) == 1 or type_cfg.get("value") in {"number", "boolean"}:
            value = int(data_hex, 16) if data_hex else 0
            if type_cfg.get("value") == "boolean":
                value = bool(value)
        elif type_cfg.get("value") == "string":
            value = raw_data.decode("utf-8", errors="replace")

        kind = PACKET_DATA
        if type_cfg.get("progress") and len(raw_data) == 1:
            kind = PACKET_COMPLETED if value == 100 else PACKET_PROGRESS
        if type_cfg == self.config["type"].get("stop") and len(raw_data) == 0:
            kind = "stop"

        parsed: dict[str, Any] = {
            "header": header,
            "type": type_code,
            "name": type_cfg.get("name", ""),
            "data": value,
            "length": len(raw_data),
            "valid": self.checksum(bytes.fromhex(body)) == int(hex_payload[-2:], 16),
        }

        if len(raw_data) > 1 and type_cfg.get("struct") and type_cfg.get("value") in self.config:
            parsed[type_cfg["value"]] = self._decode_struct(
                self.config[type_cfg["value"]], raw_data, type_cfg.get("length")
            )

        return ReceivedPacket(kind, type_code, value, payload, parsed)

    def _encode(self, type_cfg: dict[str, Any] | None, data: Any) -> Any:
        if data is True:
            return [1]
        if data is False:
            return [0]
        if isinstance(data, int):
            return [data]
        if type_cfg and type_cfg.get("struct") and isinstance(data, dict):
            return self._encode_struct(self.config[type_cfg["value"]], data, type_cfg.get("length"))
        return data

    def _encode_struct(self, type_cfg: dict[str, Any], struct: dict[str, Any], length: int | None) -> bytes:
        max_idx = max(v["index"] for v in type_cfg.values() if isinstance(v, dict) and v.get("index", -1) >= 0)
        size = length or (max_idx + 1)
        buf = bytearray(size)
        for entry in type_cfg.values():
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index", -1)
            if idx >= 0 and (length is None or idx < length):
                buf[idx] = int(struct.get(entry["name"], 0))
        return bytes(buf)

    def _decode_struct(self, type_cfg: dict[str, Any], raw: bytes, length: int | None) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in type_cfg.values():
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index", -1)
            if idx >= 0 and (length is None or idx < length):
                out[entry["name"]] = raw[idx]
        return out

    async def send(self, command: dict[str, Any] | str | bytes) -> None:
        await self._transport.write(self.packet_command(command))

    async def perform(
        self,
        command: dict[str, Any],
        receive: dict[str, Any] | None = None,
        limit: int | bool = False,
        check: bool = True,
        block: bool = True,
        wait: bool = True,
        timeout: int | None = None,
    ) -> ReceivedPacket | None:
        if check and (not self.connected() or self.busy() or not self.ready()):
            return None

        receive = receive or {}
        if block and command.get("block", True):
            self.status = STATUS_BUSY
            if wait:
                await self.wait(self.config["duration"].get("warmup", 0))

        kind = receive.get("kind", command.get("receiveKind", PACKET_DATA))
        expected_type = receive.get("type") or command.get("receiveType") or command["type"]
        expected_type = expected_type.get("code") if isinstance(expected_type, dict) else expected_type
        timeout_ms = timeout if timeout is not None else command.get("timeout", self.config["duration"].get("timeout", 0))

        await self.send(command)

        packet = None
        if kind != PACKET_NONE:
            packet = await asyncio.wait_for(
                self._wait_packet(expected_type, kind),
                timeout=timeout_ms / 1000 if timeout_ms and timeout_ms > 0 else None,
            )

        if limit:
            wait_ms = command.get("time", 0) if limit is True else int(limit)
            if wait_ms > 0:
                await self.wait(wait_ms)
                await self.stop()

        if block and command.get("block", True):
            self.status = STATUS_READY
            if wait:
                await self.wait(self.config["duration"].get("cooldown", 0))

        if "status" in command:
            self.status = command["status"]
        return packet

    async def _wait_packet(self, type_code: str, kind: str) -> ReceivedPacket:
        while True:
            packet = await self._queue.get()
            if packet.type == type_code and packet.kind == kind:
                return packet

    async def call(self, command: dict[str, Any], receive: dict[str, Any] | None = None) -> ReceivedPacket | None:
        return await self.perform(command, receive=receive, check=False, block=False, wait=False)

    def commands(self, name: str | None = None, types: list[str] | None = None) -> dict[str, dict[str, Any]] | dict[str, Any] | None:
        types = types or []
        all_commands: dict[str, dict[str, Any]] = {}
        for group, group_cfg in self.config["command"].items():
            if not isinstance(group_cfg, dict):
                continue
            if group_cfg.get("group"):
                for cmd_name, cmd in group_cfg.items():
                    if isinstance(cmd, dict) and (not types or cmd.get("type") in types):
                        all_commands[cmd_name] = cmd
            elif not types or group_cfg.get("type") in types:
                all_commands[group] = group_cfg

        if name is None:
            return all_commands

        if name in all_commands:
            return all_commands[name]

        wanted = name.lower()
        return next((c for c in all_commands.values() if c.get("name", "").lower() == wanted), None)

    async def command(self, name: str, args: list[Any] | None = None, types: list[str] | None = None, limit: bool = False):
        cmd, param = self._lookup_command(name, types)
        if cmd is None:
            return None
        if cmd.get("check", True) and (not self.connected() or self.busy() or not self.ready()):
            return None
        if cmd.get("stop"):
            return await self.stop()

        data = param if param is not None else (args[0] if args else cmd.get("data"))
        if "func" in cmd:
            fn = getattr(self, self.to_snake_case(cmd["func"]), None) or getattr(self, cmd["func"], None)
            fn_args = args or ([data] if data is not None else [])
            if cmd.get("id"):
                return await fn(cmd["id"], *fn_args)
            return await fn(*fn_args)

        timeout = (
            cmd.get("duration", 0) + self.config["duration"].get("buffer", 0)
            if cmd.get("duration", 0) > 0
            else cmd.get("timeout", self.config["duration"].get("timeout", 0))
        )
        packet = await self.perform(
            {**cmd, "data": data},
            receive={"kind": cmd.get("receiveKind", PACKET_COMPLETED)},
            limit=limit,
            timeout=timeout,
        )
        if cmd.get("end"):
            await self.end()
        return packet

    def _lookup_command(self, name: str, types: list[str] | None):
        command = self.commands(name, types)
        parameter = None
        if command is None and " " in name:
            parts = name.split(" ")
            v = parts[-1]
            c = self.commands(" ".join(parts[:-1]), types)
            if isinstance(c, dict) and c.get("parameter"):
                command = c
                parameter = self._parse(v)
        return command, parameter

    def _parse(self, raw: str) -> Any:
        if raw == "true":
            return True
        if raw == "false":
            return False
        if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
            return int(raw)
        return raw

    async def stop(self):
        if not self.connected() or not self.busy() or self.status == STATUS_STOP:
            return None
        self.status = STATUS_STOP
        await self.call(self.config["command"]["System"]["Stop"], {"type": self.config["type"]["handshake"]["code"]})
        self.status = STATUS_READY

    async def handshake(self):
        return await self.call(self.config["command"]["System"]["Handshake"])

    async def move(self, direction: str, time_ms: int | None = None):
        command = self.config["command"]["Move"].get(direction)
        if command is None:
            return None
        if time_ms != 0:
            dflt = command.get("time", self.config["command"]["Move"].get("time", 2000))
            min_t = command.get("min", self.config["command"]["Move"].get("min", 500))
            max_t = command.get("max", self.config["command"]["Move"].get("max", 10000))
            time_ms = max(min(time_ms or dflt, max_t), min_t)
        return await self.perform(
            command,
            receive={"kind": command.get("receiveKind", PACKET_NONE)},
            limit=time_ms,
            timeout=0,
        )

    async def move_forward(self, time_ms: int | None = None):
        return await self.move("Move Forward", time_ms)

    async def move_backward(self, time_ms: int | None = None):
        return await self.move("Move Backward", time_ms)

    async def turn_left(self, time_ms: int | None = None):
        return await self.move("Turn Left", time_ms)

    async def turn_right(self, time_ms: int | None = None):
        return await self.move("Turn Right", time_ms)

    async def move_left(self, time_ms: int | None = None):
        return await self.move("Move Left", time_ms)

    async def move_right(self, time_ms: int | None = None):
        return await self.move("Move Right", time_ms)

    def _normalize_joint_value(self, joint: dict[str, Any], value: Any) -> int:
        current = self.joint_current.get(joint["name"], joint["value"])
        if isinstance(value, str):
            if value.endswith("%"):
                pct_raw = value.strip()[:-1]
                if pct_raw.startswith("+") or pct_raw.startswith("-"):
                    pct = max(min(int(pct_raw), 100), -100)
                    value = current + (pct / 100.0) * (joint["max"] - joint["min"])
                else:
                    pct = max(min(int(pct_raw), 100), 0)
                    value = joint["min"] + (pct / 100.0) * (joint["max"] - joint["min"])
            else:
                value = current + int(value)
        elif isinstance(value, int) and value < 0:
            value = current + value
        elif value is None:
            value = current
        if not isinstance(value, (int, float)):
            value = current
        return int(max(min(value, joint["max"]), joint["min"]))

    async def move_joint(self, name: str, value: Any = None, speed: int | None = None, norm: bool = False):
        joint = self.config["joint"].get(name)
        if not joint:
            return None
        return await self.move_joints({joint["name"]: joint["value"] if value is None else value}, speed=speed, norm=norm)

    async def move_joints(self, joints: dict[str, Any], speed: int | None = None, norm: bool = False):
        data = {}
        for n, joint in self.config["joint"].items():
            data[n] = self._normalize_joint_value(joint, joints.get(n))
        data["speed"] = speed or joints.get("speed") or self.config["joint"]["speed"]["value"]
        await self.perform({"type": self.config["type"]["jointMove"]["code"], "data": data}, receive={"kind": PACKET_NONE}, wait=False)
        self.joint_current = {k: v for k, v in data.items() if k in self.config["joint"]}
        if norm:
            for n, j in self.config["joint"].items():
                if j.get("norm", True):
                    data[n] -= j["value"]
        await self.wait(self.config["duration"].get("delay", 0))
        return data

    async def lock_joints(self, joints: dict[str, Any]):
        data: dict[str, int] = {}
        for j in self.config["joint"].values():
            if not isinstance(j, dict) or not j.get("joint"):
                continue
            data[j["name"]] = 1 if joints.get(j["name"], self.lock_current.get(j["name"], 0)) else 0
        await self.call({"type": self.config["type"]["jointLock"]["code"], "data": data})
        self.lock_current = data
        await self.wait(self.config["duration"].get("delay", 0))
        return data

    async def unlock_all_joints(self):
        await self.call({"type": self.config["type"]["jointUnlockAll"]["code"]}, receive={"kind": PACKET_NONE})
        for k in self.lock_current:
            self.lock_current[k] = 0
        return self.lock_current

    async def lock_all_joints(self):
        await self.call({"type": self.config["type"]["jointLockAll"]["code"]}, receive={"kind": PACKET_NONE})
        for k in self.lock_current:
            self.lock_current[k] = 1
        return self.lock_current

    async def sync_joints(self):
        packet = await self.call({"type": self.config["type"]["jointSync"]["code"]}, receive={"kind": PACKET_DATA})
        if packet and "joint" in packet.parsed:
            self.joint_current = packet.parsed["joint"]
        return self.joint_current

    async def run_moves(self, moves: list[dict[str, Any]]):
        await self.lock_all_joints()
        await self.wait(self.config["duration"].get("buffer", 0))
        for move in moves:
            if move.get("wait", 0) > 0:
                await self.wait(move["wait"])
            else:
                await self.move_joints(move)
        return self.joint_current

    async def record(self, file: str = "demo"):
        file_name = file if file.endswith(".json") else f"{file}.json"
        folder = self.config["command"]["System"]["Record"]["folder"]
        path = Path.cwd() / folder / self.code / file_name
        if path.exists():
            raise FileExistsError(path)
        self._record_file = path
        self._record_moves = [
            {
                **{k: v for k, v in self.joint_initial.items() if k in self.config["joint"]},
                "speed": self.config["joint"]["speed"]["value"],
            }
        ]

    async def sync(self):
        joints = await self.sync_joints()
        self._record_moves.append({**joints, "speed": self.config["joint"]["speed"]["value"]})
        return joints

    async def pause(self, milliseconds: int = 1000):
        self._record_moves.append({"wait": int(milliseconds)})

    async def save(self, mode: str = "full"):
        moves = self._record_moves
        if mode in {"delta", "change"}:
            prev = {}
            reduced = []
            for m in moves:
                if "wait" in m:
                    reduced.append(m)
                    continue
                out = {}
                for j in self.config["joint"].values():
                    n = j["name"]
                    if prev.get(n) is None:
                        out[n] = m[n]
                    elif abs(prev[n] - m[n]) > 5:
                        if mode == "delta":
                            out[n] = m[n]
                        else:
                            diff = m[n] - prev[n]
                            out[n] = f"+{diff}" if diff > 0 else str(diff)
                out["speed"] = m.get("speed", self.config["joint"]["speed"]["value"])
                reduced.append(out)
                prev = m
            moves = reduced
        self._record_file.parent.mkdir(parents=True, exist_ok=True)
        self._record_file.write_text(json.dumps(moves, indent=2, ensure_ascii=False), encoding="utf-8")
        self._record_file = None
        self._record_moves = None

    async def run(self, file: str):
        file_name = file if file.endswith(".json") else f"{file}.json"
        folder = self.config["command"]["System"]["Run"]["folder"]
        path = Path.cwd() / folder / self.code / file_name
        moves = json.loads(path.read_text(encoding="utf-8"))
        return await self.run_moves(moves)

    async def list(self, type_code: str):
        receive = {"kind": PACKET_DATA, "type": self.config["type"]["done"]["code"]}
        await self.call({"type": type_code}, receive)
        items = []
        while not self._queue.empty():
            p = await self._queue.get()
            if p.type == type_code:
                items.append(p.data)
        return items

    async def state(self):
        p = await self.call(self.config["command"]["Info"]["State"], receive={"kind": PACKET_DATA})
        return p.parsed.get("state") if p else None

    async def volume(self, level: int):
        cfg = self.config["type"]["volume"]
        level = max(min(level, cfg.get("max", 140)), cfg.get("min", 0))
        await self.call({**self.config["command"]["Sound"]["Volume"], "data": level})
        return level

    async def audio(self, name: str):
        await self.call({**self.config["command"]["Sound"]["Audio"], "data": name})

    async def play(self, number: int):
        await self.call({**self.config["command"]["Sound"]["Play"], "data": number})

    async def toggle(self, type_code: str, value: bool):
        await self.call({"type": type_code, "data": bool(value)}, receive={"kind": PACKET_NONE})

    async def auto_stand(self, value: bool):
        await self.toggle(self.config["type"]["autoStand"]["code"], value)

    async def auto_off(self, value: bool):
        await self.toggle(self.config["type"]["autoOff"]["code"], value)

    async def auto_turn(self, value: bool):
        await self.toggle(self.config["type"]["autoTurn"]["code"], value)

    async def auto_pose(self, value: bool):
        await self.toggle(self.config["type"]["autoPose"]["code"], value)

    def to_snake_case(self, text: str) -> str:
        out = []
        for i, ch in enumerate(text):
            if ch.isupper() and i > 0 and text[i - 1].islower():
                out.append("_")
            elif ch in " -/":
                out.append("_")
                continue
            out.append(ch.lower())
        return "".join(out).strip("_").replace("__", "_")

    async def command_prompt(self, prompt: str):
        llm = self._get_llm()
        commands = list(self.commands().values())
        command_items = [c for c in commands if c.get("type") in {"17", "01", "02", "03", "05", "07", "08"}]
        user_prompt = (
            self._llm_prompt("command", "user")
            .replace("{{prompt}}", prompt)
            .replace(
                "{{commands}}",
                "\n".join(
                    f"- {c['name']} (description: {c.get('description','')}, duration: {c.get('duration','?')} ms)"
                    for c in command_items
                ),
            )
        )
        result = llm.responses.create(
            model=self.config["llm"]["model"]["default"],
            input=[
                {"role": "system", "content": self._llm_prompt("command", "system")},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = result.output_text
        data = json.loads(text)
        for selected in data.get("commands", []):
            await self.command(selected["name"], limit=True)

    async def joint_prompt(self, prompt: str):
        llm = self._get_llm()
        move = {j["name"]: j["value"] for j in self.config["joint"].values() if j.get("joint")}
        move["speed"] = self.config["joint"]["speed"]["value"]
        system_prompt = (
            self._llm_prompt("joint", "system")
            .replace("{{move}}", json.dumps(move, ensure_ascii=False))
            .replace(
                "{{joints}}",
                "\n".join(
                    f"- {j['name']} (value: {j['value']}, min: {j['min']}, max: {j['max']})"
                    for j in self.config["joint"].values()
                    if j.get("joint")
                ),
            )
        )
        result = llm.responses.create(
            model=self.config["llm"]["model"]["default"],
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": self._llm_prompt("joint", "user").replace("{{prompt}}", prompt)},
            ],
        )
        data = json.loads(result.output_text)
        if data.get("moves"):
            await self.run_moves(data["moves"])

    async def prompt(self, prompt: str, scope: str = "command"):
        if scope in {"joint", "j"}:
            return await self.joint_prompt(prompt)
        return await self.command_prompt(prompt)

    def _get_llm(self):
        if self._llm is None:
            from openai import OpenAI

            self._llm = OpenAI()
        return self._llm

    def _llm_prompt(self, scope: str, role: str) -> str:
        rel = self.config["llm"][scope][f"{role}Prompt"]
        p = Path(__file__).resolve().parent.parent / "src" / self.code / rel
        return p.read_text(encoding="utf-8")
