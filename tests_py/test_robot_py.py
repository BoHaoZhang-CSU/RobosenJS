from robosen_py import K1
from robosen_py.transports import MockTransport


def test_packet_roundtrip_state_struct():
    robot = K1({"transport": MockTransport()})
    body = {k: 0 for k in robot.config["state"].keys()}
    body["volume"] = 88
    payload = robot.packet(robot.config["type"]["state"]["code"], body)
    parsed = robot.parse_packet(payload)
    assert parsed.type == robot.config["type"]["state"]["code"]
    assert parsed.parsed["state"]["volume"] == 88
    assert parsed.parsed["valid"] is True


def test_dynamic_command_aliases_exist():
    robot = K1({"transport": MockTransport()})
    assert hasattr(robot, "left_punch")
    assert hasattr(robot, "move_forward")
    assert callable(robot.left_punch)
