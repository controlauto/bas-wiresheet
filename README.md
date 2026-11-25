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
  **Control** any output or variable directly on the controller (Manual with ON/OFF or a value, or back to Auto), also
  points that have no Home Assistant entity; **Read from Controller** lists all 64 outputs, 64 inputs and 128 variables.
- **Programs**: read all 16 programs, view the Control Basic listing, edit and compile it, compare with the controller,
  update the running program (typed confirmation), restore the previous version, start, stop and remove.
- **Program Watch**: every night (and on request) all programs are read and stored (last 20 versions); a change made
  outside the panel (for example in T3000) is recorded with the listing difference and raised as a notification.
- **Schedules**: weekly routines (Monday to Sunday plus two holiday days, 4 ON/OFF pairs per day, with a timeline) and
  annual routines (holiday calendar), edited on the controller and read back.
- **PID loops**: the 16 T3000 PID controllers (input, setpoint, output, P, I, D, bias, action, auto/manual) with tuning.
- **Export**: programs as T3000 program-editor text files (`PRGnn.txt`, *Load File* in T3000) plus exact byte code.
- **Ports**: panel number and the RS-485 ports (Modbus Master, Modbus Slave, BACnet MS/TP, unused) with baud rate.
- **Backups**: a full backup of a controller in seconds (16 programs, all inputs, outputs and variables, PID loops,
  schedules, trend log definitions and settings); up to 12 per controller with notes, pinning and JSON download.
  **Compare** two backups field by field (with the program listing difference) and **restore** selected items, each one
  read back; a backup is taken automatically before every restore. Network settings are never restored.
- **Alarms**: the controller's own alarm table (firmware and program alarms) with Acknowledge and Delete; Home Assistant
  polls it (every 60 s by default, `alarm_poll_s`), keeps one sensor per controller with the active alarm count and
  raises a notification for each new alarm.
- **Trend Logs**: the controller's 12 monitors (inputs and interval), with charts of their points from the recorder.
- **Live values in code**: the Control Basic listing with the current value of every point next to each line (manual
  points highlighted), the program's local variables and timers; refreshed every 5 s, read only.
- **Discovery**: Who-Is through the BACnet/IP integration; TEMCO controllers on BACnet/IP and MS/TP.

**Wire sheets**
- 40 blocks in 10 families, each with its own colour and symbol: inputs, T3000 points, logic (AND, OR, NOT, XOR, SR latch,
  edge pulse), math (add … moving average, rate of change, counter), control (limit, scale, hysteresis, select, PI,
  rate limit), timers, Modbus, data quality (freshness), alarms (range check, alarm with delay and severity) and displays.
- **Modbus Read / Modbus Write** blocks for any Modbus RTU device (meters, drives, other brands) on a controller port in
  Modbus Master mode: holding and input registers, coils, discrete inputs and 32-bit floats in any byte order, with scaling.
  They compile to Control Basic remote points (`<panel>.<slave>.MB_REG<n>`).
- **Send to Controller**: one step from a T3000 sheet — saves a revision, compiles, shows the change against the
  controller, then loads or replaces the program, verifies it, keeps a backup and starts it.
- **Controller faceplates** (*Controller Inputs* and *Controller Outputs*): the controller drawn as terminal strips with
  the point label, live value, status light and hand/auto switch per terminal, plus its ports (Ethernet, COM0-COM2 with
  mode and baud rate) and status lights. Wire straight to a terminal: a wire from an input terminal reads that input, a
  wire into an output terminal writes that output (same Control Basic as T3000 Read / Write). Terminals follow the
  model: T3-BB 32 IN / 12 DO / 12 AO, T3-LB 16 / 6 / 4, T3-TB 8 / 8 / 6, T3-TB-11I 11 / 6 / 5, TSTAT10 8 / 5 / 2.
  *Wired only* hides unused terminals.
- Orthogonal **auto-routed wires** (or curved), **Tidy Layout**, and blocks that show the point label.
- **T3000 sheets** compile to Control Basic and load to a controller program; generated code is checked to survive
  T3000's listing round trip. A sheet can also be **generated from an existing controller program**.
- **Monitoring sheets** use Home Assistant entities: data freshness from `last_reported`, logic and alarms.
- **Live Monitoring**: T3000 sheets show controller values and the program's own timers and local variables; monitoring
  sheets run in the browser, in Node-RED, or **continuously inside Home Assistant** (one status sensor per sheet,
  notifications for alarms and lost data). The Python and JavaScript runtimes are tested to give identical results.
- Simulation, revisions, per-sheet edit permissions, trends from the recorder, printable documentation, templates.

## Requirements

Check these before installing. The first four are needed for the integration to work; the rest are needed only for the
features named.

### Required

| What | Why | Notes |
|---|---|---|
| **Home Assistant 2026.2 or later** | Minimum version declared to HACS. | Tested on 2026.11. Home Assistant OS, Supervised, Container and Core all work if the network points below are met. |
| **[BACnet IP integration](https://github.com/CervezaStallone/Home-Assistant-BACnet-integration)** (custom integration, domain `bacnet`, installed from HACS) | Provides the controllers as Home Assistant devices and entities: point values, value writes from the Explorer, the poll heartbeat (`*_last_successful_poll` sensor) and Who-Is discovery. | Tested with version 1.0.56. Add each TEMCO controller in that integration first. The BACnet object name and description are used as the point label and description. |
| **TEMCO Controls T3 controllers on BACnet/IP** | The panel works only with TEMCO controllers (manufacturer contains "TEMCO"). Programs, point configuration, schedules, PID loops and ports use TEMCO's T3000 private transfer. | Tested on T3-LB and T3-BB. T3-TB, T3-TB-11I and TSTAT10 are recognised for port classification. Controllers on **BACnet MS/TP** need a BACnet/IP-to-MS/TP router (see *MS/TP*). |
| **Direct UDP/IP access from the Home Assistant host to each controller (port 47808)** | Program, configuration and control commands are sent straight from Home Assistant to the controller, not through the BACnet IP integration. | Same IP network or routed without NAT; no firewall blocking UDP 47808 in either direction. With Home Assistant Container, use host networking (`network_mode: host`). Discovery also needs BACnet broadcasts to reach the network. |

### Needed for some features

| What | Needed for | Without it |
|---|---|---|
| **x86-64 (Intel/AMD) Linux host** | The bundled T3000 compiler and interpreter (`bin/t3000c`, `bin/t3000vm`, static 32-bit x86 builds). They compile T3000 sheets, show program listings, edit and compare program code, generate sheets from programs, export programs and send programs to a controller. | On ARM hosts (for example Raspberry Pi) these features report a compiler error. The Explorer (points, Control, Configure), schedules, PID loops, ports, monitoring sheets, history and trends still work. |
| **Recorder** (Home Assistant default) | Trends in the inspector and point history. | No trend charts. |
| **Full access** in the configuration (`access: full` per device or `full_access_all_devices: true`) | Writing programs to any slot, controlling physical outputs, point configuration, schedules, PID tuning and port changes. | Restricted mode: program read where enabled, writes only to listed program slots and sandbox points. |
| **Administrator or listed editor account** | Any change (sheets, programs, points). | Other users can view only. |
| **A controller RS-485 port in Modbus Master mode**, wired to the Modbus RTU devices with matching baud rate and unique slave IDs | Modbus Read / Modbus Write blocks. | The program loads, but the Modbus points have no data. Set the port in Explorer > Ports. |
| **Node-RED add-on or server** with `node-red-contrib-home-assistant-websocket` (`nodered_test` setting) | Running monitoring sheets in Node-RED (Deploy to Test). | Monitoring sheets can still run in the browser or continuously inside Home Assistant. |
| **A modern desktop browser** (Chrome, Edge, Firefox, Safari) | The panel (wire sheet editor and Explorer). | The mobile app works for viewing; editing is designed for a desktop screen. |

### Before the first change on a real controller

- Back up the controller programs (Explorer > Programs > Export, or T3000).
- Know which points a Home Assistant automation or another BACnet client also writes: they overwrite manual values.
- Try a change on a test program slot and sandbox points first (`sandbox:` and `program_write_slots:` in restricted mode).

## Installation

1. HACS > Integrations > ⋮ > *Custom repositories* > add this repository, category *Integration*.
2. Install **BAS Wire Sheet for TEMCO** and restart Home Assistant.
3. *Settings > Devices & services > Add integration > BAS Wire Sheet*. The options menu sets access, each controller,
   MS/TP routers, locked equipment and Node-RED; changes apply without a restart (except the Program Watch time).
   Or use YAML: add the configuration below (for example in `packages/bas_wiresheet.yaml`) and restart again.
   When YAML configuration is present it takes precedence and the UI options are not used.

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
    alarm_poll_s: 60                # controller alarm table poll interval, 0 = off
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

`node --test tests/*.test.js tests/*.test.mjs` and `pytest tests/test_runtime_parity.py` (random sheets and telemetry run through both runtimes).

## License

MIT. Third-party components: see `THIRD_PARTY_NOTICES.md` and `THIRD_PARTY_T3000_LICENSE.txt`.
This project is not affiliated with TEMCO Controls.
