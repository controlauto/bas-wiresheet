# BAS Wire Sheet for TEMCO

A Home Assistant panel for **TEMCO Controls** T3 controllers: visual logic sheets (wire sheets), a T3000 Control Basic
compiler, program upload and editing, a point explorer, live monitoring and change history. BACnet/IP controllers
and TEMCO controllers on **BACnet MS/TP** behind a BACnet router are supported.

> This integration can change controller programs and point values that drive real equipment. Every change needs an
> editor account and a typed confirmation, is read back from the controller and is recorded in History. Configure
> access deliberately (see *Access and safety*).

## Features

**Controllers (TEMCO only)**
- **Explorer**: inputs, outputs and variables of each controller with value, units, auto/manual, port type, freshness.
  Write values; edit description, label, auto/manual, and variable type and units (T3000 point tables, read back to verify).
- **Programs**: read all 16 programs, view the Control Basic listing, edit and compile it, compare with the controller,
  update the running program (typed confirmation), restore the previous version, start, stop and remove.
- **Program Watch**: every night (and on request) all programs are read and stored (last 20 versions); a change made
  outside the panel (for example in T3000) is recorded with the listing difference and raised as a notification.
- **Schedules**: weekly routines (Monday to Sunday plus two holiday days, 4 ON/OFF pairs per day, with a timeline) and
  annual routines (holiday calendar), edited on the controller and read back.
- **PID loops**: the 16 T3000 PID controllers (input, setpoint, output, P, I, D, bias, action, auto/manual) with tuning.
- **Export**: programs as T3000 program-editor text files (`PRGnn.txt`, *Load File* in T3000) plus exact byte code.
- **Discovery**: Who-Is through the BACnet/IP integration; TEMCO controllers on BACnet/IP and MS/TP.

**Wire sheets**
- 36 blocks in 9 families, each with its own colour and symbol: inputs, T3000 points, logic (AND, OR, NOT, XOR, SR latch,
  edge pulse), math (add … moving average, rate of change, counter), control (limit, scale, hysteresis, select, PI,
  rate limit), timers, data quality (freshness), alarms (range check, alarm with delay and severity) and displays.
- **T3000 sheets** compile to Control Basic and load to a controller program; generated code is checked to survive
  T3000's listing round trip. A sheet can also be **generated from an existing controller program**.
- **Monitoring sheets** use Home Assistant entities: data freshness from `last_reported`, logic and alarms.
- **Live Monitoring**: T3000 sheets show controller values and the program's own timers and local variables; monitoring
  sheets run in the browser, in Node-RED, or **continuously inside Home Assistant** (one status sensor per sheet,
  notifications for alarms and lost data). The Python and JavaScript runtimes are tested to give identical results.
- Simulation, revisions, per-sheet edit permissions, trends from the recorder, printable documentation, templates.

## Requirements

- Home Assistant 2026.2 or later.
- A BACnet/IP integration that exposes the controllers as devices with entities (domain `bacnet`), used for values,
  writes and discovery. Program and configuration access talks to the controllers directly (T3000 private transfer).
- Linux x86-64 Home Assistant host for the bundled compiler (`bin/t3000c`, `bin/t3000vm`, static musl builds).

## Installation

1. HACS > Integrations > ⋮ > *Custom repositories* > add this repository, category *Integration*.
2. Install **BAS Wire Sheet for TEMCO** and restart Home Assistant.
3. Add the configuration below (for example in `packages/bas_wiresheet.yaml`) and restart again.

## Configuration

```yaml
bas_wiresheet:
  admins_are_editors: true          # administrators may edit; other editors by user id below
  editors: []
  t3000:
    full_access_all_devices: false  # true: every TEMCO controller, all programs and points (see Access and safety)
    unlock_protected_points: false  # true: safety-pattern points and locked equipment may be changed too
    program_watch: true
    program_watch_time: "02:30:00"
    mstp_routers:                   # BACnet network number -> router IP[:port], for MS/TP controllers
      2001: 192.0.2.20
    locked_equipment:               # optional: equipment to lock by name pattern (labels, entity ids, programs)
      - name: "Boiler 2 commissioning"
        pattern: "blr[_ -]?2"
    devices:
      123456:                       # BACnet device instance
        model: T3-LB                # T3-BB, T3-LB, T3-TB, T3-TB-11I or TSTAT10 (port classification)
        access: restricted          # or full
        allow_program_read: true
        program_write_slots: [16]
        allow_point_writes: true
        sandbox: [VAR120-128]       # points writable in restricted mode
        locked: [OUT1-10]           # never writable
        address: 192.0.2.10         # optional; MS/TP: "192.0.2.20/2001:5" (router / network:MAC)
```

## Access and safety

- **Restricted** (default): program read where enabled, writes only to listed program slots, point writes only to
  sandbox points on virtual ports.
- **Full** (`access: full` or `full_access_all_devices`): all program slots and every point, physical outputs included.
- Safety points (interlock, freeze, lockout, high/low limit, emergency stop, fire, smoke) and `locked_equipment` stay
  locked unless `unlock_protected_points: true`. A device's `locked:` list always applies.
- Points that a Home Assistant automation also writes are overwritten by it at its next cycle.

## MS/TP

TEMCO controllers on MS/TP are reached through a BACnet/IP router (for example a T3 controller acting as router).
List the router per network under `mstp_routers`, or give the device `address: router_ip/network:mac`.

## Building from source

- Panel: `cd frontend && npm install && npm run build` (writes `custom_components/bas_wiresheet/frontend/bas-wiresheet-panel.js`).
- Compiler: `bin/t3000c` and `bin/t3000vm` are built from TEMCO's T3000 source (MIT) for Linux x86-64 (musl, static).

## Tests

`node --test tests/*.test.js` and `pytest tests/test_runtime_parity.py` (random sheets and telemetry run through both runtimes).

## License

MIT. Third-party components: see `THIRD_PARTY_NOTICES.md` and `THIRD_PARTY_T3000_LICENSE.txt`.
This project is not affiliated with TEMCO Controls.
