"""T3000 program transfer over BACnet/IP ConfirmedPrivateTransfer (standard library only).

Wire format from T3000 global_function.cpp (WriteProgramData / GetProgramData) and ptransfer.c:
vendor 260, service 1, one octet string = 7-byte little-endian header + data.
Program code moves in 400-byte packets; the packet number is in the top 7 bits of entitysize.
"""
from __future__ import annotations

import hashlib
import socket
import struct
import time
from dataclasses import dataclass

VENDOR_ID = 260
SERVICE_NUMBER = 1
SERVICE_PRIVATE_TRANSFER = 18
SERVICE_READ_PROPERTY = 12
READPROGRAM = 7
READPROGRAMCODE = 16
WRITEPROGRAM = 107
WRITEPROGRAMCODE = 116
PACKET = 400
CODE_SIZE = 2000
PROGRAM_POINT_SIZE = 37
ON_OFF_OFFSET = 32
HEADER = struct.Struct("<HBBBH")


class BacnetError(Exception):
    """Transport or protocol failure."""


class UploadRefused(Exception):
    """A safety check stopped the upload before anything was written."""


def header(total: int, command: int, slot: int, entity: int) -> bytes:
    return HEADER.pack(total, command, slot, slot, entity)


def code_entity(packet: int) -> int:
    return PACKET | (packet << 9)


def packets_for(length: int) -> int:
    """Same packet count T3000 uses: (length / 401) + 1."""
    return length // (PACKET + 1) + 1


def _octets(data: bytes) -> bytes:
    n = len(data)
    if n < 5:
        return bytes([0x60 | n]) + data
    if n < 254:
        return bytes([0x65, n]) + data
    return bytes([0x65, 254]) + struct.pack(">H", n) + data


def private_params(payload: bytes) -> bytes:
    return bytes([0x0A]) + struct.pack(">H", VENDOR_ID) + bytes([0x19, SERVICE_NUMBER, 0x2E]) + _octets(payload) + bytes([0x2F])


def bvll(apdu: bytes, expect_reply: bool = True, dnet: int | None = None, dadr: bytes = b"") -> bytes:
    """BVLC unicast + NPDU. With dnet/dadr the request is routed (for example to a TEMCO controller on MS/TP behind a router)."""
    if dnet is None:
        npdu = bytes([1, 0x04 if expect_reply else 0]) + apdu
    else:
        npdu = bytes([1, 0x20 | (0x04 if expect_reply else 0)]) + struct.pack(">HB", dnet, len(dadr)) + dadr + bytes([0xFF]) + apdu
    return bytes([0x81, 0x0A]) + struct.pack(">H", len(npdu) + 4) + npdu


def parse_route(text: str) -> tuple[str, int, int | None, bytes]:
    """'192.0.2.10', '192.0.2.10:47808' or routed '192.0.2.20:47808/2001:5' (router / network:MS/TP MAC)."""
    ip_part, _, route = str(text).partition("/")
    host, _, port = ip_part.partition(":")
    if not route:
        return host, int(port or 47808), None, b""
    net, _, mac = route.partition(":")
    if not net.isdigit() or not mac:
        raise ValueError(f"Bad routed address {text!r}; use router_ip[:port]/network:mac")
    dadr = bytes.fromhex(mac[2:]) if mac.lower().startswith("0x") else bytes([int(mac)])
    return host, int(port or 47808), int(net), dadr


def strip_bvll(frame: bytes) -> bytes:
    if len(frame) < 6 or frame[0] != 0x81 or frame[1] != 0x0A or struct.unpack(">H", frame[2:4])[0] != len(frame):
        raise BacnetError("Not a BACnet/IP unicast frame")
    if frame[4] != 1:
        raise BacnetError("Unsupported NPDU version")
    ctrl, pos = frame[5], 6
    if ctrl & 0x80:
        raise BacnetError("Network layer message")
    if ctrl & 0x20:
        pos += 3 + frame[pos + 2]
    if ctrl & 0x08:
        pos += 3 + frame[pos + 2]
    if ctrl & 0x20:
        pos += 1
    return frame[pos:]


def parse_private(body: bytes) -> bytes:
    """Return the octet string of a private transfer request/ack ('' when the result block is absent)."""
    if len(body) < 5 or body[0] != 0x0A or struct.unpack(">H", body[1:3])[0] != VENDOR_ID or body[3] != 0x19 or body[4] != SERVICE_NUMBER:
        raise BacnetError("Unexpected private transfer identity")
    pos = 5
    if pos >= len(body):
        return b""
    if body[pos] != 0x2E:
        raise BacnetError("Malformed private transfer block")
    pos += 1
    if pos < len(body) and body[pos] == 0x2F:
        return b""
    tag = body[pos]
    if tag >> 4 != 6:
        raise BacnetError("Private transfer data is not an octet string")
    pos += 1
    n = tag & 7
    if n == 5:
        n = body[pos]
        pos += 1
        if n == 254:
            n = struct.unpack(">H", body[pos:pos + 2])[0]
            pos += 2
        elif n == 255:
            raise BacnetError("Octet string too long")
    data = body[pos:pos + n]
    if len(data) != n or body[pos + n:pos + n + 1] != b"\x2F":
        raise BacnetError("Truncated private transfer data")
    return data


def code_used_length(code: bytes) -> int:
    """Used bytes from the program's own section headers; 0 for an empty or invalid slot."""
    if len(code) < 2:
        return 0
    base = code[0] | code[1] << 8
    if base == 0 or base > CODE_SIZE:
        return 0
    pos = 2 + base + 3
    for _ in range(2):
        if pos + 2 > len(code):
            return 0
        pos += 2 + (code[pos] | code[pos + 1] << 8)
    return pos if pos <= CODE_SIZE else 0


class Client:
    """Blocking BACnet/IP client for one controller."""

    def __init__(self, host: str, port: int = 47808, timeout: float = 3.0, retries: int = 3, gap: float = 0.0,
                 dnet: int | None = None, dadr: bytes = b"") -> None:
        self.addr = (host, port)
        self.dnet, self.dadr = dnet, dadr
        self.timeout, self.retries, self.gap = timeout, retries, gap
        self.invoke = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(timeout)

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _transact(self, service: int, params: bytes) -> bytes | None:
        last = "no response"
        for _ in range(self.retries):
            if self.gap:
                time.sleep(self.gap)
            self.invoke = (self.invoke + 1) & 0xFF
            invoke = self.invoke
            self.sock.sendto(bvll(bytes([0x00, 0x05, invoke, service]) + params, True, self.dnet, self.dadr), self.addr)
            try:
                while True:
                    frame, src = self.sock.recvfrom(1600)
                    if src[0] != self.addr[0]:
                        continue
                    apdu = strip_bvll(frame)
                    kind = apdu[0] >> 4
                    if kind in (2, 3, 5, 6, 7) and apdu[1] != invoke:
                        continue
                    if kind == 2 and apdu[2] == service:
                        return None
                    if kind == 3:
                        if apdu[0] & 0x08:
                            raise BacnetError("Segmented reply is not supported")
                        if apdu[2] != service:
                            raise BacnetError("Reply for a different service")
                        return apdu[3:]
                    if kind == 5:
                        raise BacnetError(f"BACnet error reply: {apdu[3:].hex()}")
                    if kind == 6:
                        raise BacnetError(f"BACnet reject, reason {apdu[2]}")
                    if kind == 7:
                        raise BacnetError(f"BACnet abort, reason {apdu[2]}")
            except socket.timeout:
                last = "timeout"
        via = f" network {self.dnet} MAC {self.dadr.hex()}" if self.dnet is not None else ""
        raise BacnetError(f"No reply from {self.addr[0]}:{self.addr[1]}{via} ({last})")

    def _private(self, payload: bytes) -> bytes:
        body = self._transact(SERVICE_PRIVATE_TRANSFER, private_params(payload))
        return b"" if body is None else parse_private(body)

    def _private_write(self, payload: bytes) -> None:
        """Any acknowledgement is accepted (real firmware returns a non-octet result); callers verify by reading back."""
        self._transact(SERVICE_PRIVATE_TRANSFER, private_params(payload))

    def identify(self) -> int:
        body = self._transact(SERVICE_READ_PROPERTY, bytes([0x0C]) + struct.pack(">I", (8 << 22) | 4194303) + bytes([0x19, 75]))
        if not body or len(body) < 12 or body[5:7] != b"\x19\x4b"[:2] or body[7] != 0x3E or body[8] != 0xC4:
            raise BacnetError("Unexpected device identity reply")
        oid = struct.unpack(">I", body[9:13])[0]
        if oid >> 22 != 8:
            raise BacnetError("Identity reply is not a device object")
        return oid & 0x3FFFFF

    def read_point(self, slot: int) -> bytes:
        data = self._private(header(PRIVATE_LEN, READPROGRAM, slot, PROGRAM_POINT_SIZE))
        if len(data) != PRIVATE_LEN + PROGRAM_POINT_SIZE or data[2] != READPROGRAM or data[3] != slot:
            raise BacnetError("Unexpected program status reply")
        return data[PRIVATE_LEN:]

    def write_point(self, slot: int, point: bytes) -> None:
        if len(point) != PROGRAM_POINT_SIZE:
            raise ValueError("Bad program status size")
        self._private_write(header(PRIVATE_LEN + PROGRAM_POINT_SIZE, WRITEPROGRAM, slot, PROGRAM_POINT_SIZE) + point)

    def program_is_on(self, slot: int) -> bool:
        return self.read_point(slot)[ON_OFF_OFFSET] != 0

    def read_code(self, slot: int) -> bytes:
        out = bytearray()
        for packet in range(CODE_SIZE // PACKET):
            data = self._private(header(PRIVATE_LEN, READPROGRAMCODE, slot, code_entity(packet)))
            if len(data) != PRIVATE_LEN + PACKET or data[2] != READPROGRAMCODE or data[3] != slot or data[6] >> 1 != packet:
                raise BacnetError(f"Unexpected program code reply for packet {packet}")
            out += data[PRIVATE_LEN:]
        return bytes(out)

    def write_packets(self, slot: int, image: bytes, count: int) -> None:
        for packet in range(count):
            chunk = image[packet * PACKET:(packet + 1) * PACKET].ljust(PACKET, b"\0")
            self._private_write(header(PRIVATE_LEN + PACKET, WRITEPROGRAMCODE, slot, code_entity(packet)) + chunk)


PRIVATE_LEN = HEADER.size


BYTES_OFFSET = 30


def status_with(point: bytes, *, on: bool | None = None, size: int | None = None) -> bytes:
    out = bytearray(point)
    if on is not None:
        out[ON_OFF_OFFSET] = 1 if on else 0
    if size is not None:
        out[BYTES_OFFSET:BYTES_OFFSET + 2] = struct.pack("<H", size)
    return bytes(out)


def run_size(used: int) -> int:
    """T3000 status field: used code length minus 9. The firmware does not run a program with 0 here."""
    return used - 9


def describe_status(point: bytes) -> dict:
    return {"description": point[:21].split(b"\0")[0].decode("latin-1"), "label": point[21:30].split(b"\0")[0].decode("latin-1"),
            "bytes": point[BYTES_OFFSET] | point[BYTES_OFFSET + 1] << 8, "on": point[ON_OFF_OFFSET] != 0,
            "auto_manual": point[33], "error_code": point[35]}


@dataclass
class Plan:
    device: int
    slot_index: int
    new_sha: str
    old_sha: str
    old_used: int
    new_length: int
    packets: int
    token: str
    old_status: str = ""


def _token(device: int, slot: int, old_sha: str, new_sha: str, old_status: str) -> str:
    return hashlib.sha256(f"{device}:{slot}:{old_sha}:{new_sha}:{old_status}".encode()).hexdigest()[:16]


def plan_upload(client: Client, expected_device: int, slot: int, allowed_slots: list[int], code: bytes, *, allow_replace: bool = False) -> tuple[Plan, bytes]:
    """Read-only checks. Slot is 1-16 as shown in T3000. Returns the plan and the backup image.

    TEMCO controllers report empty slots as ON, so an empty ON slot is accepted: apply turns it OFF first.
    """
    if not 1 <= slot <= 16:
        raise UploadRefused("Program slot must be 1-16")
    if slot not in allowed_slots:
        raise UploadRefused(f"Program slot {slot} is not in the allowed slots {allowed_slots}")
    if not code or len(code) >= CODE_SIZE or code_used_length(code) != len(code):
        raise UploadRefused("Invalid program size")
    device = client.identify()
    if device != expected_device:
        raise UploadRefused(f"Controller at this address is device {device}, expected {expected_device}")
    index = slot - 1
    status = client.read_point(index)
    backup = client.read_code(index)
    used = code_used_length(backup)
    occupied = bool(used) or any(backup)
    if occupied and status[ON_OFF_OFFSET]:
        raise UploadRefused(f"Program {slot} is ON and holds code. It must be turned OFF before an upload")
    if occupied and not allow_replace:
        raise UploadRefused(f"Program {slot} already holds {used} bytes. Replacing it needs an explicit decision")
    old_sha = hashlib.sha256(backup).hexdigest()
    new_sha = hashlib.sha256(code).hexdigest()
    return Plan(device, index, new_sha, old_sha, used, len(code), packets_for(len(code)),
                _token(device, index, old_sha, new_sha, status.hex()), status.hex()), backup


def apply_upload(client: Client, plan: Plan, code: bytes, backup: bytes, token: str) -> dict:
    """Turn OFF, write, read back, record the size, leave OFF. On failure restore code and status."""
    if token != plan.token or token != _token(plan.device, plan.slot_index, plan.old_sha, hashlib.sha256(code).hexdigest(), plan.old_status):
        raise UploadRefused("Confirmation token does not match this plan")
    if hashlib.sha256(backup).hexdigest() != plan.old_sha:
        raise UploadRefused("Backup image does not match the plan")
    if client.identify() != plan.device:
        raise UploadRefused("Controller identity changed since the plan")
    original = bytes.fromhex(plan.old_status)
    if client.read_point(plan.slot_index) != original:
        raise UploadRefused("Program status changed since the plan")
    if hashlib.sha256(client.read_code(plan.slot_index)).hexdigest() != plan.old_sha:
        raise UploadRefused("Program changed on the controller since the plan")
    image = code.ljust(plan.packets * PACKET, b"\0")
    off = status_with(original, on=False)
    try:
        client.write_point(plan.slot_index, off)
        if client.read_point(plan.slot_index) != off:
            raise BacnetError("Program did not turn OFF")
        client.write_packets(plan.slot_index, image, plan.packets)
        after = client.read_code(plan.slot_index)
        if after[:len(image)] != image:
            raise BacnetError("Read-back does not match the program sent")
        final = status_with(off, size=run_size(len(code)))
        client.write_point(plan.slot_index, final)
        if client.read_point(plan.slot_index) != final:
            raise BacnetError("Program size did not read back")
    except BacnetError as err:
        return {"ok": False, "error": str(err), **_restore(client, plan.slot_index, backup, original)}
    return {"ok": True, "verified_bytes": len(image), "program_left_off": True, "sha256": plan.new_sha}


def stable_status(point: bytes) -> bytes:
    """Status without the error byte, which the firmware updates while a program runs."""
    return point[:35] + point[36:]


@dataclass
class ReplacePlan:
    device: int
    slot_index: int
    new_sha: str
    old_digest: str
    old_status: str
    was_on: bool
    new_length: int
    token: str


def _replace_token(device: int, slot: int, digest: str, new_sha: str, status: str) -> str:
    return hashlib.sha256(f"replace:{device}:{slot}:{digest}:{new_sha}:{status}".encode()).hexdigest()[:16]


def plan_replace(client: Client, expected_device: int, slot: int, allowed_slots: list[int], code: bytes, digest) -> tuple[ReplacePlan, bytes]:
    """Read-only checks before changing a program that already exists on the controller."""
    if slot not in allowed_slots:
        raise UploadRefused(f"Program {slot} is not enabled for writing")
    if not code or len(code) >= CODE_SIZE or code_used_length(code) != len(code):
        raise UploadRefused("Invalid program size")
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    index = slot - 1
    status = client.read_point(index)
    backup = client.read_code(index)
    if not code_used_length(backup):
        raise UploadRefused(f"Program {slot} is empty; use Load Program")
    old = digest(backup)
    new_sha = hashlib.sha256(code).hexdigest()
    text = stable_status(status).hex()
    return ReplacePlan(expected_device, index, new_sha, old, status.hex(), bool(status[ON_OFF_OFFSET]), len(code),
                       _replace_token(expected_device, index, old, new_sha, text)), backup


def apply_replace(client: Client, plan: ReplacePlan, code: bytes, backup: bytes, token: str, digest) -> dict:
    """Stop, write, read back, record the size and return the program to its previous ON/OFF state."""
    original = bytes.fromhex(plan.old_status)
    if token != plan.token or token != _replace_token(plan.device, plan.slot_index, plan.old_digest, hashlib.sha256(code).hexdigest(), stable_status(original).hex()):
        raise UploadRefused("Confirmation token does not match this plan")
    if client.identify() != plan.device:
        raise UploadRefused("Controller identity changed since the check")
    current = client.read_point(plan.slot_index)
    if stable_status(current) != stable_status(original):
        raise UploadRefused("Program status changed since the check")
    if digest(client.read_code(plan.slot_index)) != plan.old_digest:
        raise UploadRefused("Program changed on the controller since the check")
    image = code.ljust(CODE_SIZE, b"\0")
    off = status_with(current, on=False)
    try:
        client.write_point(plan.slot_index, off)
        if client.read_point(plan.slot_index)[ON_OFF_OFFSET] != 0:
            raise BacnetError("Program did not turn OFF")
        client.write_packets(plan.slot_index, image, CODE_SIZE // PACKET)
        if client.read_code(plan.slot_index) != image:
            raise BacnetError("Read-back does not match the program sent")
        final = status_with(off, on=plan.was_on, size=run_size(len(code)))
        client.write_point(plan.slot_index, final)
        after = client.read_point(plan.slot_index)
        if bool(after[ON_OFF_OFFSET]) != plan.was_on or after[BYTES_OFFSET:BYTES_OFFSET + 2] != final[BYTES_OFFSET:BYTES_OFFSET + 2]:
            raise BacnetError("Program status did not read back")
    except BacnetError as err:
        return {"ok": False, "error": str(err), **_restore(client, plan.slot_index, backup, original)}
    return {"ok": True, "verified_bytes": len(image), "running": plan.was_on, "sha256": plan.new_sha}


def _restore(client: Client, index: int, backup: bytes, original: bytes) -> dict:
    """Code first, status last: if the code cannot be restored the program stays OFF."""
    try:
        client.write_point(index, status_with(original, on=False))
        client.write_packets(index, backup, CODE_SIZE // PACKET)
        if client.read_code(index) != backup:
            return {"restored": False, "restore_error": "Code did not read back; program left OFF"}
        client.write_point(index, original)
        if stable_status(client.read_point(index)) != stable_status(original):
            return {"restored": False, "restore_error": "Status did not read back"}
        return {"restored": True, "restore_error": ""}
    except BacnetError as err:
        return {"restored": False, "restore_error": str(err)}


def start_program(client: Client, expected_device: int, slot: int, sha256: str, logic: str | None = None, digest=None) -> dict:
    """Turn ON only the program that was uploaded.

    A running or previously run program keeps its timers and local values inside the code area, so the exact hash only
    matches a never-run upload; the logic digest (when given) is compared instead of the raw bytes in that case.
    """
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    index = slot - 1
    code = client.read_code(index)
    used = code_used_length(code)
    exact = bool(used) and hashlib.sha256(code[:used]).hexdigest() == sha256
    if not exact and not (used and logic and digest and digest(code) == logic):
        raise UploadRefused("The program on the controller is not the expected one")
    status = client.read_point(index)
    if status[ON_OFF_OFFSET] and _size(status) == run_size(used):
        return {"ok": True, "on": True, "already_on": True}
    client.write_point(index, status_with(status, on=True, size=run_size(used)))
    after = client.read_point(index)
    if not after[ON_OFF_OFFSET] or _size(after) != run_size(used):
        raise BacnetError("Program did not turn ON")
    return {"ok": True, "on": True}


def _size(point: bytes) -> int:
    return point[BYTES_OFFSET] | point[BYTES_OFFSET + 1] << 8


def stop_program(client: Client, expected_device: int, slot: int) -> dict:
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    index = slot - 1
    target = status_with(client.read_point(index), on=False)
    client.write_point(index, target)
    if client.read_point(index)[ON_OFF_OFFSET] != 0:
        raise BacnetError("Program did not turn OFF")
    return {"ok": True, "on": False}


def remove_program(client: Client, expected_device: int, slot: int) -> dict:
    """Erase a stopped program and clear its size."""
    if client.identify() != expected_device:
        raise UploadRefused("Wrong controller at this address")
    index = slot - 1
    status = client.read_point(index)
    if status[ON_OFF_OFFSET]:
        raise UploadRefused("Stop the program before removing it")
    client.write_packets(index, bytes(CODE_SIZE), CODE_SIZE // PACKET)
    if any(client.read_code(index)):
        raise BacnetError("Code did not erase")
    target = status_with(status, size=0)
    client.write_point(index, target)
    after = client.read_point(index)
    if after[ON_OFF_OFFSET] or _size(after):
        raise BacnetError("Status did not read back")
    return {"ok": True}
