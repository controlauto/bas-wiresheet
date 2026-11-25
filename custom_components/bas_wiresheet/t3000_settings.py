"""Controller settings of TEMCO T3 controllers: panel number and RS-485 port configuration.

T3000 CM5/ud_str.h Str_Setting_Info (400 bytes): READ_SETTING_COMMAND = 98, write = 198.
Offsets (verified on a T3-LB and a T3-BB: IP address and panel name read back as configured):
ip 0-3, com0/1/2 mode 38-40, com0/1/2 baud 44-46, network_number 50, panel_type 51, panel_name 52-71, panel_number 73.
A port change reads the block, changes only the mode/baud bytes of that port, writes it back and reads it again.
"""
from __future__ import annotations

from typing import Any

from .t3000_bacnet import PRIVATE_LEN, BacnetError, UploadRefused, header

READ_CMD, SIZE = 98, 400
PORT_MODE_OFFSET, PORT_BAUD_OFFSET = 38, 44
PANEL_NUMBER_OFFSET, PANEL_NAME = 73, (52, 20)
PORTS = ["COM0", "COM1", "COM2"]
# T3000 global_define.h Device_Serial_Port_Status / CM5 com type enum
PORT_MODES = ["Unused", "BACnet MS/TP", "Modbus Slave", "BACnet PTP", "GSM", "Main Zigbee", "Sub Zigbee", "Modbus Master", "RS232 Meter",
              "BACnet MS/TP Master"]
EDITABLE_MODES = {0: "Unused", 1: "BACnet MS/TP", 2: "Modbus Slave", 7: "Modbus Master"}
BAUD_RATES = [1200, 2400, 3600, 4800, 7200, 9600, 19200, 38400, 76800, 115200, 921600, 57600]


def read_raw(client) -> bytes:
    data = client._private(header(PRIVATE_LEN, READ_CMD, 0, SIZE))
    if len(data) != PRIVATE_LEN + SIZE or data[2] != READ_CMD:
        raise BacnetError("Unexpected reply reading the controller settings")
    return data[PRIVATE_LEN:]


def decode(raw: bytes) -> dict[str, Any]:
    ports = []
    for i, name in enumerate(PORTS):
        mode, baud = raw[PORT_MODE_OFFSET + i], raw[PORT_BAUD_OFFSET + i]
        ports.append({"port": name, "mode_code": mode, "mode": PORT_MODES[mode] if mode < len(PORT_MODES) else f"code {mode}",
                      "baud_code": baud, "baud": BAUD_RATES[baud] if baud < len(BAUD_RATES) else None})
    s0, sl = PANEL_NAME
    return {"panel": raw[PANEL_NUMBER_OFFSET], "panel_name": raw[s0:s0 + sl].split(b"\0")[0].decode("latin-1"),
            "ip": ".".join(str(b) for b in raw[0:4]), "ports": ports}


def read_settings(client, expected_device: int) -> dict[str, Any]:
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    return decode(read_raw(client))


def set_port(client, expected_device: int, port: str, mode: int | None = None, baud: int | None = None) -> dict[str, Any]:
    """Change one RS-485 port's mode and/or baud rate (mode by T3000 code, baud in bit/s). Read back to verify."""
    if port not in PORTS:
        raise UploadRefused(f"Unknown port {port}")
    if mode is not None and mode not in EDITABLE_MODES:
        raise UploadRefused(f"Port mode must be one of: {', '.join(EDITABLE_MODES.values())}")
    if baud is not None and baud not in BAUD_RATES:
        raise UploadRefused(f"Baud rate must be one of {', '.join(map(str, sorted(BAUD_RATES)))}")
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    before = read_raw(client)
    i = PORTS.index(port)
    buf = bytearray(before)
    if mode is not None:
        buf[PORT_MODE_OFFSET + i] = mode
    if baud is not None:
        buf[PORT_BAUD_OFFSET + i] = BAUD_RATES.index(baud)
    if bytes(buf) == before:
        return {"ok": True, "changed": False, "before": decode(before), "after": decode(before)}
    client._private_write(header(PRIVATE_LEN + SIZE, READ_CMD + 100, 0, SIZE) + bytes(buf))
    after = read_raw(client)
    if after[PORT_MODE_OFFSET + i] != buf[PORT_MODE_OFFSET + i] or after[PORT_BAUD_OFFSET + i] != buf[PORT_BAUD_OFFSET + i]:
        raise BacnetError(f"{port}: the controller did not keep the new port settings")
    return {"ok": True, "changed": True, "before": decode(before), "after": decode(after)}
