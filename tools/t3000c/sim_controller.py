"""Simulated T3000 controller for upload tests. Loopback only; it never touches a real device."""
from __future__ import annotations

import importlib.util
import socket
import struct
import threading
from pathlib import Path

_p = Path(__file__).resolve().parents[2] / "custom_components" / "bas_wiresheet" / "t3000_bacnet.py"
_s = importlib.util.spec_from_file_location("t3000_bacnet_sim", _p)
tb = importlib.util.module_from_spec(_s)
import sys  # noqa: E402
sys.modules["t3000_bacnet_sim"] = tb
_s.loader.exec_module(tb)


class SimController:
    def __init__(self, instance: int = 999001, port: int = 0, route: tuple[int, int] | None = None,
                 routed_source: tuple[int, bytes] | None = None) -> None:
        self.route = route          # (network, MS/TP MAC): behave like a controller behind a BACnet router
        # (network, MAC): answer plain unicast requests with a routed source, as a Tstat10 on Wi-Fi does
        # (seen on a Tstat10 on Wi-Fi); HA's BACnet/IP integration drops such replies
        self.routed_source = routed_source
        self.instance = instance
        self.code = [bytearray(tb.CODE_SIZE) for _ in range(16)]
        self.on = [False] * 16
        self.points = [bytearray(tb.PROGRAM_POINT_SIZE) for _ in range(16)]
        self.point_writes: list[int] = []
        self.ignore_status_write = False
        self.fail_erase = False
        self.writes: list[tuple[int, int]] = []
        self.fail_write_packet: int | None = None   # reject this packet once
        self.corrupt_write_once = False             # store one wrong byte once
        self.runtime_state = False
        # point tables: OUT (cmd 1, 45 bytes), IN (cmd 2, 46 bytes), VAR (cmd 3, 39 bytes)
        self.tables = {1: [bytearray(45) for _ in range(64)], 2: [bytearray(46) for _ in range(64)], 3: [bytearray(39) for _ in range(128)],
                       4: [bytearray(28) for _ in range(16)], 5: [bytearray(42) for _ in range(8)], 6: [bytearray(33) for _ in range(4)],
                       17: [bytearray(144) for _ in range(8)], 18: [bytearray(46) for _ in range(4)], 41: [bytearray([255] * 72) for _ in range(8)]}
        st = bytearray(400); st[0:4] = bytes([127, 0, 0, 1]); st[38:41] = bytes([7, 0, 1]); st[44:47] = bytes([6, 6, 6])
        st[52:55] = b"SIM"; st[73] = 4
        self.tables[98] = [st]
        self.tables[13] = [bytearray(102) for _ in range(16)]   # alarm log (Alarm_point)
        self.tables[10] = [bytearray(104) for _ in range(12)]   # monitors (Str_monitor_point)                      # controller settings (Str_Setting_Info): panel 4, COM0 Modbus Master, COM2 MS/TP
        self.no_flags = False                       # emulate firmware without the schedule flag table
        self.table_writes = []                  # like real firmware: a running program changes its timer bytes and error byte
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(2)
        self.sock.close()

    def _loop(self):
        while not self._stop.is_set():
            try:
                frame, addr = self.sock.recvfrom(1600)
            except socket.timeout:
                continue
            ctrl = frame[5] if len(frame) > 5 else 0
            dest = None
            if ctrl & 0x20:
                dnet = int.from_bytes(frame[6:8], "big")
                dadr = frame[9:9 + frame[8]]
                dest = (dnet, dadr)
            if self.route is not None and dest != (self.route[0], bytes([self.route[1]])):
                continue            # not addressed to this routed station
            if self.route is None and dest is not None:
                continue
            apdu = tb.strip_bvll(frame)
            if apdu[0] != 0:
                continue
            invoke, service, body = apdu[2], apdu[3], apdu[4:]
            reply = self._handle(invoke, service, body)
            if reply is not None:
                if self.route is None and self.routed_source is not None:
                    net, mac = self.routed_source
                    npdu = bytes([1, 0x08]) + net.to_bytes(2, "big") + bytes([len(mac)]) + mac + reply
                    self.sock.sendto(bytes([0x81, 0x0A]) + (len(npdu) + 4).to_bytes(2, "big") + npdu, addr)
                elif self.route is None:
                    self.sock.sendto(tb.bvll(reply, expect_reply=False), addr)
                else:   # router forwards the reply with the source network and MAC
                    npdu = bytes([1, 0x08]) + self.route[0].to_bytes(2, "big") + bytes([1, self.route[1]]) + reply
                    self.sock.sendto(bytes([0x81, 0x0A]) + (len(npdu) + 4).to_bytes(2, "big") + npdu, addr)

    def write_ack(self, invoke, service):
        """Like DA firmware 0.8.0: a ComplexACK whose result block is not an octet string."""
        return bytes([0x30, invoke, service, 0x0A, 0x01, 0x04, 0x19, 0x01, 0x2E, 0x21, 0x01, 0x2F])

    def _tick(self, slot):
        """Change a runtime value the way DA firmware does: the first timer's millisecond count, else a local's value."""
        code = self.code[slot]
        base = code[0] | code[1] << 8
        if not base or base > tb.CODE_SIZE - 8:
            return
        pos = 2 + base + 3
        size = code[pos] | code[pos + 1] << 8
        timers = pos + 2 + size
        tsize = code[timers] | code[timers + 1] << 8
        if tsize:
            at = code.find(b"\xff", timers + 2, timers + 2 + tsize) + 2   # delimiter, state byte, then the count
        elif size:
            at = pos + 2 + 1                                              # first local: type byte, then its value
        else:
            return
        if 0 < at < tb.CODE_SIZE:
            code[at] = (code[at] + 1) & 0xFF

    def _handle(self, invoke, service, body):
        if service == tb.SERVICE_READ_PROPERTY:
            oid = struct.pack(">I", (8 << 22) | self.instance)
            return bytes([0x30, invoke, service, 0x0C]) + oid + bytes([0x19, 75, 0x3E, 0xC4]) + oid + bytes([0x3F])
        if service != tb.SERVICE_PRIVATE_TRANSFER:
            return bytes([0x60, invoke, 9])
        data = tb.parse_private(body)
        total, command, start, end, entity = tb.HEADER.unpack(data[:7])
        ack = lambda payload: bytes([0x30, invoke, service]) + tb.private_params(payload)
        if command % 100 in self.tables and not (self.no_flags and command % 100 == 41):
            base = command % 100
            table = self.tables[base]
            size = len(table[0])
            if command < 100:   # range reads (start..end), like real T3 firmware
                if end < start or end >= len(table) or entity != size:
                    return bytes([0x60, invoke, 4])
                body = b"".join(bytes(table[k]) for k in range(start, end + 1))
                return ack(tb.HEADER.pack(7 + len(body), command, start, end, entity) + body)
            if start != end or start >= len(table) or entity != size:
                return bytes([0x60, invoke, 4])
            if total != 7 + size or len(data) != 7 + size:
                return bytes([0x60, invoke, 4])
            table[start] = bytearray(data[7:])
            self.table_writes.append((base, start))
            return self.write_ack(invoke, service)
        if start != end or start > 15:
            return bytes([0x60, invoke, 4])
        if command == tb.READPROGRAM:
            point = bytearray(self.points[start])
            point[tb.ON_OFF_OFFSET] = 1 if self.on[start] else 0
            if self.runtime_state and self.on[start]:
                point[35] = (point[35] + 1) & 0xFF
                self._tick(start)
            return ack(tb.header(7 + len(point), command, start, entity) + bytes(point))
        if command == 107:
            if total != 44 or len(data) != 44 or entity != tb.PROGRAM_POINT_SIZE:
                return bytes([0x60, invoke, 4])
            self.point_writes.append(data[7 + tb.ON_OFF_OFFSET])
            if not self.ignore_status_write:
                self.points[start] = bytearray(data[7:])
                self.on[start] = data[7 + tb.ON_OFF_OFFSET] != 0
            return self.write_ack(invoke, service)
        packet = entity >> 9
        if entity & 0x1FF != tb.PACKET or packet > 4:
            return bytes([0x60, invoke, 4])
        if command == tb.READPROGRAMCODE:
            return ack(tb.header(7 + tb.PACKET, command, start, entity) + bytes(self.code[start][packet * 400:(packet + 1) * 400]))
        if command == tb.WRITEPROGRAMCODE:
            if total != 407 or len(data) != 407:
                return bytes([0x60, invoke, 4])
            if self.fail_write_packet == packet:
                self.fail_write_packet = None
                return bytes([0x70, invoke, 4])
            chunk = bytearray(data[7:])
            if self.fail_erase and not any(chunk):
                return bytes([0x20, invoke, service])
            if self.corrupt_write_once:
                self.corrupt_write_once = False
                chunk[5] ^= 0xFF
            self.code[start][packet * 400:(packet + 1) * 400] = chunk
            self.writes.append((start, packet))
            return self.write_ack(invoke, service)
        return bytes([0x60, invoke, 9])
