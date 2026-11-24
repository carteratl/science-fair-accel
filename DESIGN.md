# Science-fair accelerometer logger: design

Status: the open questions are answered (2026-11-23) and v1 is being built from this design. Section 0 sets the defaults for this experiment.

---

## 0. This experiment

**Setup:** an earthquake simulation. A plexiglass shaker table rides on rubber-ball bearings and is held by rubber bands. A Duplo tower stands on the table. You pull the table and release it.

**Sensors:** two per run.

| Port | Name | Mounted on | Measures |
|---|---|---|---|
| A | `Base` | its own Duplo brick, snapped onto the table | the "earthquake" (ground motion) |
| B | `Top` | its own Duplo brick, snapped onto the top of the tower | how the building responds |

Each module is glued to its own Duplo brick, so it can be pulled off for calibration and snapped back on.

**Hardware and access:**
- Raspberry Pi 5 Model B (8 GB), reached over SSH.
- Sensors within 30 cm of the Pi, so the default 400 kHz bus speed applies.

**Recording:** 100 samples/s. You start and stop each run by hand, so the default duration is 0, meaning record until you press S.

**What this changes in the design:**
- **Ports:** A + B, exactly as in 2.2. No breadboard and no AD0 jumper. Port C stays supported but unused.
- **New CSV column `<Name>_horiz_g`.** This is the sideways (horizontal) shaking with gravity removed, measured relative to how the sensor sat during "HOLD STILL". It doesn't depend on which way the sensor is glued, so Base and Top can be compared directly. See 8.1.
- **Earthquake numbers in the summary and in `runs.csv`, for each sensor:**
  - peak horizontal acceleration, and when it happened
  - RMS (average) horizontal acceleration
  - Top ÷ Base peak ratio (the *amplification*)
- **Calibration becomes 6-position** (see 5.6). You pull the sensor's brick off and rest it on each of its 6 faces. The horizontal scale matters for the Top ÷ Base ratio.
- **Space marks the release.** The simulator also starts a simulated shake when you press Space, so the whole flow can be tried without hardware.
- **Pi 5 details:**
  - Bus 1 is the RP1 controller, and the software bus 3 runs through RP1's GPIO pins.
  - Both are expected to work. The bench and the first-day checks confirm it on your Pi.
  - The I2C error codes differ from older Pis, which is why any OSError counts as a failed read.
- **Mounting tips for this rig:**
  - Glue each module square onto a flat side of its own Duplo brick. Snap the brick on the same way round every time, ideally with one printed axis arrow pointing in the pull direction.
  - Push the brick on fully and check it doesn't rock; a loose fit rattles and adds spikes.
  - Keep each brick's cable on the same Pi pins, because calibration is stored per port.
  - Leave a loop of slack in each cable so the table and the tower can move freely.
  - Tape the plugs at the Pi end.
  - If the tower falls, Top's cable must not yank the Pi or the table.

---

## 1. At a glance

- **Two Python 3 command-line programs** on the Pi:
  - **`accel-setup`** (program 1) finds the sensors, initializes them, and shows live readings. It also lets you name each sensor and calibrate it.
  - **`accel-record`** (program 2) records one trial per run to an Excel-ready CSV, then offers the next trial.
- **Dependencies are apt packages only:** `python3-smbus2`, `i2c-tools`, `tmux`. No pip, because Raspberry Pi OS blocks system-wide pip (PEP 668). These packages exist in both Bookworm and Trixie.
- **Up to 3 sensors on fixed ports A, B and C.** A port is a wiring position: an I2C bus plus an address. The MPU-6050 has no serial number, so friendly names belong to ports.
- **One CSV per trial.** `time_s` is in column A, followed by 8 columns per sensor (including `<Name>_horiz_g`). In Excel, select the columns and use Insert → Scatter with Straight Lines.
- **Defaults:** 100 samples/s per sensor, ±8 g, ±1000 °/s.
- **Raw data is kept.** The raw integer readings and the run details are saved next to every CSV. Any trial can be checked or converted again, and a crash never loses more than about 2 s.

---

## 2. Hardware

### 2.1 Why three sensors need two I2C buses

An MPU-6050 answers at only two addresses: **0x68** (AD0 pin low) or **0x69** (AD0 pin high). So at most two can share one I2C bus. The third needs a second bus.

**Chosen:** a software I2C bus (the kernel `i2c-gpio` overlay) on GPIO23/24. One config line gives the same pins and the same `/dev/i2c-3` on a Pi 3, Zero 2 W, 4 or 5, and it needs no extra parts. It runs at about 100 kHz, which is plenty for one sensor at 100 Hz.

Rejected:
- **TCA9548A multiplexer:** an extra part. It can be added later with only a config change.
- **AD0 chip-select trick:** needs GPIO-switching code and reads the sensors one after another.
- **Pi 4/5 hardware buses:** their pins differ by model.

### 2.2 Ports and wiring

The GY-521 header pins are labelled **VCC GND SCL SDA XDA XCL AD0 INT**. On the module, SCL comes before SDA. On the Pi, SDA (pin 3) comes before SCL (pin 5). Swapping these two is the most common wiring mistake.

| Port | Used when you have | Bus / address | VCC | GND | SDA | SCL | AD0 |
|---|---|---|---|---|---|---|---|
| **A** | 1, 2 or 3 sensors | i2c-1 / 0x68 | pin 1 (3.3 V) | pin 6 | pin 3 (GPIO2) | pin 5 (GPIO3) | not connected (the board pulls it low) |
| **B** | 2 or 3 sensors | i2c-3 / 0x68 | pin 17 (3.3 V) | pin 20 | pin 16 (GPIO23) | pin 18 (GPIO24) | not connected |
| **C** | 3 sensors | i2c-1 / 0x69 | pin 1, shared with A | pin 9 | pin 3, shared with A | pin 5, shared with A | **solder a short jumper on the module from VCC to AD0** |

- **One or two sensors (A, or A + B):** every wire goes straight from the module to the Pi header. No breadboard and no shared pins.
- **Three sensors:** a mini breadboard joins A and C on pins 1, 3 and 5.
- **Why A + B for two sensors:** it needs no AD0 wiring, and one bus fault can't take out both sensors.
- **Two sensors at 200 Hz:** use A + C instead (see 7.3).

**Pi header pins used.** Pin 1 has the square solder pad, at the end of the header farthest from the USB ports.
```
  used by          pin  pin          used by
  A + C   3.3V    [ 1] [ 2]  5V      never use
  A + C   SDA     [ 3] [ 4]  5V      never use
  A + C   SCL     [ 5] [ 6]  GND     A
                  [ 7] [ 8]
  C       GND     [ 9] [10]
                  [11] [12]
                  [13] [14]
                  [15] [16]  GPIO23  B SDA
  B       3.3V    [17] [18]  GPIO24  B SCL
                  [19] [20]  GND     B
```

### 2.3 Wiring rules

- **Power every module from 3.3 V (pins 1 and 17), never 5 V.**
  - This works whether or not a particular board has its own regulator.
  - Module C's AD0 jumper is only safe at 3.3 V. Label module C **"C · 0x69 · 3.3 V ONLY"**.
- **Wire with the Pi powered off.**
- **Solder the header pins onto the modules.** Loose pins pushed into the holes make contact on and off.
- Leave XDA, XCL and INT unconnected.
- **Lead length:**
  - Keep leads to **30 cm or less** at the default 400 kHz.
  - For leads up to about 1 m, set bus 1 to 100 kHz and twist each signal wire with a ground wire.
  - If the sensors travel farther than that, put the Pi and a USB power bank on the moving object (question Q3).
- **Mount each module rigidly** with double-sided foam tape or a zip-tie. A loose module rattles, and the rattle shows up as fake spikes. Write down which way the board's printed X/Y arrows point.
- **Label each module and both ends of its cable** A, B or C.

### 2.4 Pi configuration (the installer does this)

Append these lines under `[all]` in `/boot/firmware/config.txt` (or `/boot/config.txt` on older images):
```
dtparam=i2c_arm=on
dtparam=i2c_arm_baudrate=400000
dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24,i2c_gpio_delay_us=2
```

The file must **not** contain either of these, because each would claim address 0x68 (i2cdetect shows `UU`):
- `dtoverlay=i2c-sensor,mpu6050`, the kernel's own MPU-6050 driver
- an `i2c-rtc` overlay, for a real-time clock chip

---

## 3. Software structure

```
accel/                      source (built here, copied to the Pi)
  install.sh                one-time Pi setup (section 10)
  README.md                 wiring, experiment-day checklist, Excel steps, "what the numbers mean"
  accel_setup.py            -> /usr/local/bin/accel-setup
  accel_record.py           -> /usr/local/bin/accel-record
  accellib/
    mpu.py       chip table, register init, burst read, unit conversion, DLPF tables per chip family
    ports.py     preflight checks, scan, opening buses (with a short I2C timeout)
    config.py    sensors.json load/atomic save, name rules, single-instance lock
    capture.py   sampler thread, writer thread, deadline scheduler
    convert.py   raw -> Excel CSV, summary statistics, runs.csv
    term.py      plain-ANSI screen, non-blocking keys, prompts
    sim.py       FakeBus simulated sensors for testing without hardware
  tests/
~/accel-data/               everything the user copies off the Pi
  sensors.json  sensors.json.bak
  runs.csv                  one row per trial
  ramp30_t01_20261123-1405.csv
  details/  ramp30_t01_20261123-1405_raw.csv  ..._info.json  error.log
```

- **Two commands, one per program you asked for.** They share `accellib`.
- **No `rich` and no numpy.** That means one fewer dependency, and the code behaves the same on Bookworm (Python 3.11) and Trixie (Python 3.13).
- **Only one of the two programs can run at a time.** Both hold a lock on `~/accel-data/.lock`. Setup re-initializes the sensors, so it must never run during a recording. A second copy exits with "accel-setup is already running; quit it first."
- **Neither program runs as root.** If a student retries with `sudo`, the data ends up owned by root or in the wrong home folder. Both programs refuse and print the fix (log out and back in to pick up the `i2c` group).
- **The data folder can be moved** with `--data-dir` or the `ACCEL_DATA_DIR` environment variable, for example to a USB stick. The config file lives in the same folder.
- **Scripted runs:** when stdin is not a terminal, `accel-record` needs `--label` and `--duration` greater than 0. It runs one trial with no prompts. The end-to-end tests use this.

---

## 4. Config file: `~/accel-data/sensors.json`

```json
{
  "schema": 1,
  "rate_hz": 100, "accel_range_g": 8, "gyro_range_dps": 1000,
  "data_dir": "~/accel-data",
  "last": {"label": "ramp30", "duration_s": 10},
  "ports": {
    "A": {"bus": 1, "addr": "0x68", "name": "Cart",  "chip_id": "0x68",
          "cal": {"date": "2026-11-23", "chip_id": "0x68", "accel_offset_g": [0.021, -0.012, 0.043],
                  "accel_scale": [1.004, 0.997, 1.012], "gyro_bias_dps": [-1.21, 0.84, 0.30]}},
    "B": {"bus": 3, "addr": "0x68", "name": "Wheel", "chip_id": "0x98", "cal": null},
    "C": {"bus": 1, "addr": "0x69", "name": null,    "chip_id": null,   "cal": null}
  }
}
```

- **Saving:** automatic after every change. The program writes a temp file, fsyncs it, renames it into place, and keeps a `.bak`. If the file can't be read, the program stops and points to the `.bak`. It never silently resets the names.
- **Names and trial labels:**
  - Letters, digits and `_` only, starting with a letter, 12 characters at most.
  - Unique.
  - Not date-like: Excel turns `MAR1` or `Sep2` into dates.
- **Calibration belongs to a port.** If a module moves to a different port, recalibrate it. The stored `chip_id` only catches a swap between *different* chip types (two genuine MPU-6050s both read 0x68), so the ready screen's tap check is the real swap guard.

---

## 5. Program 1: `accel-setup`

1. **Preflight checks.** Each problem prints a one-line fix:
   - `/dev/i2c-1` missing: run the installer.
   - Permission denied: log out and back in.
   - `/dev/i2c-3` missing: port B is off, with an overlay hint.
   - Address busy: the program names the kernel driver holding it, read from `/sys/bus/i2c/devices/1-0068/name`.
2. **Scan.** Only buses 1 and 3, only addresses 0x68 and 0x69. Each device is identified by reading WHO_AM_I (register 0x75), never by i2cdetect-style quick writes.
   - `0x68`: MPU-6050. A genuine part reads 0x68 even when it sits at address 0x69; that is normal.
   - `0x70 / 0x71 / 0x73 / 0x74`: MPU-6500 family. `0x98 / 0xAF`: ICM family. These dies are common on cheap boards. They are accepted with a note, and the program uses that family's filter table.
   - Anything else: "Unknown device 0xNN, try anyway? [y/N]". This catches, for example, a clock chip at 0x68.
   - Mixed chip families: warning. Their filters delay the signal by different amounts, so the sensors would be offset in time by up to tens of ms.
3. **Initialize and health-check.** Initialize every sensor (section 7), then run a 1 s hands-off check. The status is **CHECK** if any of these is true:
   - an axis is stuck at full scale
   - readings are frozen
   - more than 5% of reads fail
   - total acceleration is outside 0.85–1.15 g at rest

   The hint suggests trying another module, since cheap boards are sometimes bad.
4. **Live screen.** Redraws 5 times a second, showing 0.2 s averages:
   ```
    ACCEL SETUP   100 Hz  ±8 g  ±1000 °/s   round 2.9 ms (max ~200 Hz)
    Port Name   Chip      ax g    ay g    az g  total g    gx     gy     gz °/s  err  Status
    A    Cart   MPU-6050  0.012  -0.004   1.003   1.003    0.1   -0.2    0.0     0  OK  cal 11-23
    B    Wheel  ICM-20689 0.501   0.020   0.861   0.997   85.3   12.0   -3.1     0  MOVING
    C    -      not found: check pins 3/5 (SDA/SCL swapped?) and C's AD0 jumper
    [N]ame  [C]alibrate  [S]ettings  [R]escan  [Q]uit        changes save automatically
   ```
5. **[N] Name by tilting.** The prompt says: *"Tilt or flip ONE sensor."*
   - The tool watches each port's gravity direction. When one port moves more than 60° and every other port moves less than 15°, it asks: *"That's port B (bus 3, 0x68). Name [Wheel]:"*
   - Then it reminds you to label that module "B – Wheel".
   - This works before calibration and needs no tuning. Tilting is gentler on jumper wires than shaking.
   - Fallback: type the port letter.
6. **[C] Calibrate (6-position).** Optional, once per module, about 2 minutes. Pull the sensor's Duplo brick off first.
   - Hold the sensor still on each of its 6 sides, in any order. With the sensor on a Duplo brick, rest the brick on each face; for the face carrying the module, press it against a box side so the pins hang free. A face counts when one axis is clearly down (within about 20°). The six readings are then fitted together (offset and scale per axis so that every reading has magnitude 1 g), which makes the result independent of small tilts and of the chip's own zero-g offsets.
   - The tool sees which side is down, captures 2 s, and ticks that side off.
   - **What it gives:** an offset and a scale for every axis, from (+1 g reading + −1 g reading) / 2 and (+1 g − −1 g) / 2. It also gives a gyro bias as a fallback (see 6.4).
   - **Checks:** a side is redone if the sensor moved. Results outside offset ±0.15 g or scale 0.90–1.10 are rejected.
   - **Why bother:**
     - Cheap MPU-6050s can be off by up to ±0.05 g (X/Y) and ±0.08 g (Z), with about ±3% scale error.
     - The Top ÷ Base ratio is only as good as the two sensors' scales.
7. **[S] Settings.**
   - Sample rate: 25, 50, 100 or 200 Hz.
   - Accel range: ±2, 4, 8 or 16 g.
   - Gyro range: ±250, 500, 1000 or 2000 °/s.

   Rate and ranges are separate settings.
8. **[R] Rescan** after fixing a wire. **[Q] Quit.**

---

## 6. Program 2: `accel-record`

Run it with no arguments and it asks for everything. Every prompt has a sensible default. The same answers can be given as flags: `--label --duration --note --rate --sim`.

1. **Start-up.** The recorder loads the config, scans, and gives every sensor identical settings. It reads the registers back to confirm them, then runs a **1 s bench** (back-to-back reads) before the first trial.
   - **Bench too slow:** if one round of reads takes more than 60% of the sample period, the recorder refuses with a clear fix: *"3 sensors can't do 200 Hz on this Pi: use 100 Hz or 2 sensors."* It never lowers the rate silently.
   - **A named sensor is missing:** `[R]etry  [C]ontinue without Cart  [Q]uit`. The choice is recorded.
   - **Port C is configured, 0x69 is missing, but 0x68 answers on bus 1:** big warning. C's AD0 jumper may have come loose. C would then be answering on A's address and corrupting A's readings. You can continue only after confirming C is unplugged.
   - **WHO_AM_I differs from the config:** "Different module or swapped cable? Recalibrate."
   - **Free disk space:** checked.
   - **Started over SSH and not inside tmux:** the recorder restarts itself inside `tmux new-session -s accel` and says so (if a session called `accel` already exists, it refuses and says to `tmux attach -t accel`). A dropped Wi-Fi connection then can't kill a run. `tmux attach -t accel` gets you back.
2. **Prompts:**
   - condition label (default: the last one)
   - trial number (default: the next unused one for that label)
   - duration in seconds (default 0 = until you press S; max 60 min)
   - note (always starts blank)
3. **Ready screen.**
   - The label and trial are shown in large text.
   - Each sensor shows a live motion level (mg). *"Gently tap the TOP of the tower: Top's motion should jump much more than Base's."* This catches swapped sensors or cables. (Tapping the table can't tell them apart, because the tower stands on it.) A sensor that stops answering shows NO DATA, and a sensor that has reset is re-initialised.
   - Press **Enter** to start.
4. **Countdown: "HOLD STILL 3… 2… 1… GO"** (with a terminal beep).
   - The countdown samples are saved in the raw file only. The CSV starts at `time_s = 0` at GO.
   - If a sensor held still (gyro scatter below 1 °/s), its gyro bias for this run comes from the countdown, because gyro bias drifts from day to day. Otherwise the stored bias is used. Either way, the info file records which source was used.
5. **Recording.** A status line, updated twice a second:
   `REC t3   4.2/10 s  99.9 Hz late 0 | Base 1.02g | Top 3.41g CLIP` (cut to the terminal width; the keys are shown on the GO screen above it)
   - **Space** adds an event mark, timestamped when the key is read (within 20 ms). Use it for "released the cart" or "hit the floor".
   - **S** stops. **Enter** also stops, but is ignored for the first second, and keys typed before GO are thrown away.
6. **Stop.** The writer finishes the raw file and the recorder converts it to the Excel CSV. It then writes the info file and the `runs.csv` row.
7. **Summary:**
   ```
   Saved h8_plain_t03_20261123-1405.csv
     1000 rows   10.00 s   99.98 Hz   late 0   (complete)
     Base       peak sideways 0.418 g at 1.59 s   rms 0.108 g   errors 0
     Top        peak sideways 0.836 g at 1.61 s   rms 0.244 g   errors 0
     (rms = the 5 s after the release mark, from 1.49 s)
     Top / Base peak ratio (amplification): 2.00
     Marks at: 1.49 s
   Verdict: GOOD
   [Enter] h8_plain trial 4   [L] new label   [B] mark this trial bad   [Q] quit
   ```
   Bad trials are kept. Marking one bad records that in `runs.csv`, so it can be excluded honestly.

---

## 7. Sensor settings and sampling

### 7.1 Initialization (each sensor; all sensors are reset together so start-up is quick)

| Step | Register ← value | Purpose |
|---|---|---|
| 1 | `0x6B ← 0x80`, wait 100 ms | Full device reset |
| 2 | `0x68 ← 0x07`, wait 100 ms | Reset the signal paths |
| 3 | `0x6B ← 0x01`, wait 100 ms | Wake up and use the gyro PLL clock (±1%, better than the internal oscillator) |
| 4 | `0x19 ← 0x00` | Internal sample rate 1 kHz, so each poll gets data no more than 1 ms old |
| 5 | `0x1A ←` DLPF code (table below) | Anti-alias low-pass filter |
| 6 | `0x1B ←` 0x00/0x08/0x10/0x18 | Gyro ±250/500/1000/2000 °/s |
| 7 | `0x1C ←` 0x00/0x08/0x10/0x18 | Accel ±2/4/8/16 g |
| 8 | `0x1D ←` accel DLPF code | **MPU-6500/ICM dies only.** These have a separate accel filter register. Verify on the real boards. |
| 9 | Read back 0x19–0x1C (masked by field), plus 0x1D where used | Any mismatch means the sensor is faulty. The first 100 ms of data is thrown away. |

**Scale factors:**
- Accel: 16384 / 8192 / 4096 / 2048 counts per g.
- Gyro: 131 / 65.5 / 32.8 / 16.4 counts per °/s.

**Low-pass filter by sample rate (MPU-6050 accel bandwidth / delay):**

| Rate | Code | Bandwidth | Delay |
|---|---|---|---|
| 25 Hz | 6 | 5 Hz | 19.0 ms |
| 50 Hz | 5 | 10 Hz | 13.8 ms |
| 100 Hz | 4 | 21 Hz | 8.5 ms |
| 200 Hz | 3 | 44 Hz | 4.9 ms |

6500/ICM dies have their own table, kept in `mpu.py`.

**Consequence:** at 100 Hz, events shorter than about 25 ms are smoothed, so short impact peaks read low. The summary and the info file say so. Drop and impact experiments need the high-rate mode (question Q2).

### 7.2 Timing

- **Polling, not the sensor's FIFO.** Each round does one 14-byte burst read per sensor (registers 0x3B–0x48: accel, temperature, gyro). One burst guarantees that all six axes come from the same instant.
- **Three threads:**
  - **Sampler:** schedules round *k* at `t0 + k × period` on `time.monotonic_ns()`, so timing never drifts. If a round runs late, the sampler skips ahead and never bursts to catch up. The gap shows honestly in `time_s`.
  - **Writer:** appends integer rows to the raw file. It flushes every 1 s and fsyncs every 2 s, so an SD-card stall never delays the sampler.
  - **Main:** handles the screen and the keys.
- **Lower jitter:** `sys.setswitchinterval(0.0005)` and `gc.freeze()` after initialization.
- **Per-sensor timing:** each sensor's exact offset within the round (about 0.5–2 ms) is stored in the raw file.

### 7.3 Expected speed (the start-up bench measures the real numbers)

| Setup | Time per round | 100 Hz | 200 Hz |
|---|---|---|---|
| A only | ~0.6 ms | easy | easy |
| A + B (bus 3 is bit-banged, ~1.8 ms) | ~2.5 ms | easy | marginal |
| A + C (both on bus 1) | ~1.3 ms | easy | easy |
| A + B + C | ~3–3.5 ms | easy | **no** |
| A + B + C with bus 1 at 100 kHz (long leads) | ~5–6 ms | OK (bench decides) | no |

---

## 8. Output files

### 8.1 Main CSV, for Excel

**Filename:** `<label>_t<NN>_<YYYYMMDD-HHMM>.csv`, for example `ramp30_t03_20261123-1405.csv`.
- The trial number is one more than the highest found in the data folder and in `runs.csv`.
- The Pi's clock can be wrong when it has no internet, so the trial number keeps names unique anyway.
- The file is never overwritten.

**Format:**
- A single header row and no lines above it. Metadata lines above the header break Excel's import.
- ASCII headers, dot decimals.

```
time_s,Base_ax_g,Base_ay_g,Base_az_g,Base_total_g,Base_horiz_g,Base_gx_dps,Base_gy_dps,Base_gz_dps,Top_ax_g,...,Top_gz_dps,mark,flags
0.0000,0.0121,-0.0043,1.0031,1.0032,0.0021,0.12,-0.20,0.03,0.0098,...,-3.10,0,
0.0100,0.0118,-0.0040,1.0029,1.0030,0.0018,0.10,-0.21,0.02,0.0102,...,-3.08,0,
```

**Columns:**

| Column | Meaning |
|---|---|
| `time_s` | Seconds since GO, from the Pi's steady clock. 4 decimals. Always column A, so it is always the X axis. |
| `<Name>_ax_g` … `_az_g` | Calibrated acceleration in g, 4 decimals. |
| `<Name>_total_g` | √(ax² + ay² + az²). It is 1.00 at rest and doesn't depend on how the sensor is turned. |
| `<Name>_horiz_g` | Sideways shaking with gravity removed, always ≥ 0. Computed as the size of (a − a₀) at right angles to gravity, where a₀ is the average during HOLD STILL. It doesn't depend on mounting direction. **This is the main earthquake column.** |
| `<Name>_gx_dps` … `_gz_dps` | Rotation rate in °/s with the bias removed, 2 decimals. |
| `mark` | Number of Space presses so far. It charts as a step at each event. |
| `flags` | Blank when everything is fine. Otherwise one or more of the flags below. |

**Flags:**
- `ERR:<Name>`: the read failed or was rejected. That sensor's cells are **left blank, never zero**, so a fault can't be mistaken for free fall.
- `CLIP:<Name>`: an accelerometer axis was at 90% or more of its range.
- `GCLIP:<Name>`: a gyro axis was at 90% or more of its range. The sideways numbers are unaffected.
- `TILT:<Name>`: the sensor has tilted or fallen (more than 15° from HOLD STILL), so its `horiz_g` includes gravity. These rows are left out of its peak and RMS.
- `LATE`: the round started more than half a period late.

**Column rules:**
- **Every configured port always gets its columns, in A/B/C order,** even if that sensor was missing (the cells are then blank). Trials of one condition always line up for copy-paste.
- **Temperature is not in this file.** It is the chip's own temperature, not the air's, and is easy to misread. It is kept in the raw file and the info file.

### 8.2 Detail files (in `details/`)

- **`<base>_raw.csv`**
  - **Contents:** integer counts exactly as read, per sensor including temperature, plus the sample index, a nanosecond timestamp (negative during the countdown), and each sensor's read offset and status.
  - **Durability:** written live and fsynced every 2 s.
  - **Purpose:** this file is the source of truth. The main CSV is rebuilt from it, including after a crash.
- **`<base>_info.json`**
  - Label, trial and note.
  - Wall-clock start, and whether the clock was synced.
  - Status: recording, complete, stopped, interrupted or disk_full.
  - Requested and achieved rate. Interval mean, standard deviation and maximum, and the number of late rounds.
  - Each sensor's port, bus, address, chip, ranges, scale factors, filter bandwidth and delay, calibration used and gyro-bias source, errors, clips, recoveries, and chip temperature at start and end.
  - Mark times.
  - Pi model, OS and Python version, the actual I2C clock read from the device tree, and the under-voltage state from `vcgencmd get_throttled`. A weak power bank can reset sensors.
  - Written at the start and replaced atomically at the end.

### 8.3 `runs.csv`: one row per trial, for a results table

```
run,file,label,trial,started,clock_synced,duration_s,rate_hz,achieved_hz,late,status,quality,note,
reference,amplification,rms_window_start_s,rms_window_s,
A_name,A_peak_horiz_g,A_peak_horiz_time_s,A_rms_horiz_g,A_peak_ratio,A_peak_total_g,A_tilted_at_s,A_errors,A_clipped,A_gyro_clipped,
B_name,...,C_gyro_clipped
```

- **`quality`:** GOOD, CHECK or BAD. BAD means you marked the trial bad.
- **`reference`:** the sensor that ratios are measured against: the one **named** Base, whatever its port (else port A).
- **`amplification`:** Top peak ÷ Base peak when exactly two sensors are present. Left blank if Base barely moved (under 0.05 g).
- **`P_peak_ratio`:** that sensor's peak divided by the reference's.
- **`P_rms_horiz_g`:** RMS over the 5 s from the Space mark (or from when Base started shaking), so it doesn't depend on when S was pressed. It is left blank when the window wasn't fully covered (stopped too soon, or that sensor fell during it), so every RMS in the table covers the same 5 s. `rms_window_start_s` and `rms_window_s` say where the window began and how much of it was recorded.
- **`P_tilted_at_s`:** when that sensor tilted or fell. Detection: its orientation moved more than 15° from HOLD STILL, judged on a 1 s average that ignores impact spikes. The start time comes from the gyro: the last moment, within 2 s before detection, when it had turned less than 7.5°. Rows after that are flagged `TILT:` and left out of its peak and RMS, because `horiz_g` then includes gravity. If it fell within 0.5 s of the release, there is no peak or ratio for that trial (note shown). A smaller lasting lean (5–15°) gets a note saying how much of `horiz_g` is gravity.
- **Why it helps:** these columns let you build a trials-by-conditions table (for example tower height or bracing) without opening every file.

---

## 9. Errors and recovery

| Situation | What happens |
|---|---|
| Any I2C error on a read | Counted as a failed read for **that sensor only**. Different buses raise different errors (121, 6, 5, 110, 11), so any OSError counts. A *fast* failure is retried once immediately. |
| A stuck bus (e.g. an unpowered module holding SCL low) | A failure that takes more than 5 ms is not retried: the sensor goes **DOWN** at once, and its probes back off from 1 s to 5 s. `I2C_RETRIES` is set to 0 on every bus. The bit-banged bus (port B) otherwise retries 3 times at ~100 ms each, so a stall drops from ~0.8 s to ~0.2 s. `I2C_TIMEOUT` (30 ms) only helps bus 1: the bit-banged bus's ~100 ms step timeout is fixed by the overlay. A dead Top therefore costs Base about one 0.2 s gap per probe. Reading each bus in its own thread would remove even that (section 15). |
| All-zero or all-0xFF frame | Rejected as a failed read. A just-reset chip or a shorted SDA line can return zeros, which would look exactly like free fall. |
| 20 or more identical frames in a row | Treated as frozen. Those rows are blanked when the CSV is built. |
| 10 failures in a row | The sensor is marked **DOWN** and the recorder stops reading it every round. Once a second it probes WHO_AM_I. When the sensor answers, the recorder waits 100 ms, re-initializes it, verifies the read-back, and resumes. Rows in between are `ERR`. |
| A sensor resets itself (power dip) | Caught by reading back the range registers once a second, confirmed by a second read (I2C has no checksum). This works on every chip family; checking 0x6B only works on genuine MPU-6050s. The row is written with status RESET, and the converter blanks the second before it. That rule lives in the raw file, so it also works for a recovered run. The sensor is then re-initialized like DOWN. |
| S, Ctrl-C, kill, or SSH hang-up | Stop flag is set. The writer drains its queue and fsyncs, then the CSV, info file and runs.csv row are finished **before** anything is printed. If the terminal is gone, the files are still complete. Ctrl-C during those few seconds of saving is ignored, with a "still saving" message. Stopping before GO cancels the trial and saves nothing. |
| Power cut or crash | The raw file is at most 2 s behind. At the next start, any run still marked "recording" is converted and marked `interrupted`. |
| Disk nearly full | The recorder refuses to start without twice the expected file size plus 100 MB free. If the disk fills mid-run, it stops cleanly and marks the run `disk_full`. |
| Unexpected bug | One line on screen, the full traceback in `details/error.log`, and the files are still closed properly. |

The verdict is **CHECK** if:
- there are any errors or accel clips
- more than 1% of rounds were late
- the sensors' chip families are mixed
- the SD card couldn't take all the data
- Base barely moved (under 0.05 g)

The reason is spelled out. The fix is usually "redo the trial."

These are **notes**, not CHECK, because the data is still good:
- a sensor tilted or fell
- gyro clipping (`GCLIP`)
- no Space mark
- the run stopped before the 5 s RMS window ended

---

## 10. Install on a fresh Pi (do this at home, with internet, before the fair)

1. With the Pi off, solder the headers and wire the sensors (section 2). Then boot Raspberry Pi OS, Bookworm or Trixie, Desktop or Lite.
2. Copy `accel/` into your home folder (`scp -rp`) and run `sudo bash ~/accel/install.sh`. It is safe to run again. It:
   - runs `apt-get update` and `apt-get install -y python3-smbus2 i2c-tools tmux`, then checks that `import smbus2` works
   - runs `raspi-config nonint do_i2c 0` to enable I2C
   - loads `i2c-dev` at every boot. It finds config.txt, backs it up, and appends the 2.4 block once, inside markers: if the markers are already there, it leaves the file alone. It warns about conflicting overlays (mpu6050, i2c-rtc, another `i2c-gpio`, `i2c3`) and about HATs that use GPIO23/24.
   - adds your user to the `i2c` group. It finds the real user and home folder even under sudo.
   - creates `~/accel-data` owned by you and links the two commands into `/usr/local/bin`
   - offers to reboot
3. After the reboot, check the buses:
   - `ls /dev/i2c-*` shows 1 and 3.
   - `i2cdetect -y -r 1` shows 68, plus 69 with C connected.
   - `i2cdetect -y -r 3` shows 68.

   Then run `accel-setup`.
4. **Getting files to Excel:** copy `~/accel-data` with a USB stick (Desktop OS mounts it under `/media/<you>/`) or with `scp -r`. The folder holds the config too, so it is also your backup.

---

## 11. Testing without hardware

- **Simulator.** `--sim` (or `ACCEL_SIM=1`) swaps the I2C bus for `FakeBus`:
  - Three simulated chips with IDs 0x68, 0x98 and 0x68.
  - Each starts asleep until woken, and honours the range settings.
  - Output is gravity, plus about 3 mg of noise, plus offsets and gyro bias.
  - Keys 1–3 tilt a sensor, for testing naming.
  - smbus2 is imported only when needed, so the simulator runs on a laptop.
- **Fault injection.** `--sim-faults` adds:
  - read errors with a mix of errno 121, 6, 5, 110 and 11
  - port B dropping out from 3 s to 5 s
  - port C resetting to zeros at 4 s
  - a frozen sensor
  - clipping
- **Unit tests** (`python3 -m unittest`):
  - byte unpacking at the edge values
  - scale factors for every range
  - DLPF tables
  - name rules (`MAR1` rejected)
  - trial numbering
  - CSV header and format against a reference file
  - the scheduler with a fake clock (late rounds counted, no catch-up bursts)
  - atomic config save, and refusing a corrupt config
  - clean shutdown on each signal, including with the terminal gone
  - rebuilding an interrupted run
- **End-to-end.** `accel-record --sim --duration 5` should give 500 rows, an achieved rate within 0.5% of 100 Hz, and the scripted bump in the right columns.
- **First-day hardware checks:**
  - flat reads +1 g and flipped reads −1 g (±0.05 before calibration)
  - a 10-minute soak test at your chosen rate gives 0 late rounds and 0 errors
  - pulling port B's SDA wire mid-run gives ERR, then DOWN, then recovery
  - Ctrl-C keeps the file
  - the CSV charts in Excel

---

## 12. Experiment-day checklist (also goes in the README)

1. Wiring check: every module is labelled, its plugs are taped, and it is mounted rigidly.
2. `accel-setup`: all sensors show **OK**. Calibrate any module that moved to a different port.
3. Over SSH? Just run `accel-record`; it starts tmux for you.
4. For each trial, enter the label, check the trial number, and tap the tower top to check the names. Then HOLD STILL → GO → do the experiment → S.
5. Read the verdict. If it says CHECK, fix the cause and redo the trial.
6. At the end of the day, copy `~/accel-data` off the Pi.

**What the numbers mean** (README section):
- An accelerometer always feels gravity. A sensor at rest reads `total_g` = 1.00, and in free fall it reads ≈ 0.
- A cart rolling freely down a 30° ramp reads about 0 g *along* the ramp and about 0.87 g total, not 0.5 g.
- Which axis is which depends on how the board is mounted. That is why the mounting direction gets written down.

**Excel:**
1. Open the CSV.
2. Select `time_s` plus the columns you want.
3. Insert → **Scatter with Straight Lines**, not a Line chart. A Line chart spaces the points evenly and ignores the real times.

If your Excel uses a comma as the decimal separator, use Data → From Text/CSV instead of double-clicking.

---

## 13. Open questions

**Answered 2026-11-23** (see section 0):
- Q1: Pi 5, used over SSH.
- Q2: shake table, which counts as motion. No FIFO mode.
- Q3: sensors within 30 cm.
- Q4: start and stop by hand.

**Still open, with defaults:**
- Q5, soldering: the header pins still need soldering.
- Q6, units: g only.
- Q7, getting files to Excel: `scp` (you already use SSH), with dot decimals.

The original questions:

1. **Pi model and OS?** (Pi 3/4/5/Zero 2 W; Bookworm or Trixie; Desktop or Lite.) Will you use a monitor and keyboard, or SSH from a laptop?
2. **What is the experiment?** Motion (ramp, pendulum, vehicle, swing) or impact (drop, crash, egg-drop)? Roughly what peak g?
   - **Motion:** fits the design as it stands.
   - **Impact:** needs a **high-rate mode**: one sensor at 1000 Hz using the chip's FIFO, with a wider filter and ±16 g. That adds about a day of work, and I would build it into v1.
3. **Will the sensors move away from the Pi, and how far?** Up to 30 cm: default. Up to about 1 m: slower bus. Farther: the Pi rides along on a USB power bank.
4. **Typical run length?** Fixed duration, stop-when-done, or both? (Both are supported. This only sets the default.)
5. **Can you solder?** The header pins and module C's AD0 jumper need it.
6. **Units:** g only, or m/s² columns too?
7. **How will files get to Excel?** USB stick, scp or network share? Windows or Mac Excel? Comma or dot decimals where you live?

---

## 14. Key decisions

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Third sensor | Software I2C bus 3 on GPIO23/24 | TCA9548A mux; AD0 switching; Pi 4/5 hardware bus | Same on every Pi, no extra parts, no GPIO code |
| Port order | A = bus 1 @ 0x68, B = bus 3 @ 0x68, C = bus 1 @ 0x69 | Two sensors both on bus 1 | 1–2 sensors need no AD0 wiring or breadboard |
| C's address | Jumper soldered on the module from VCC to AD0 | Loose AD0 wire to the Pi | A loose AD0 wire silently corrupts sensor A |
| Power | 3.3 V | 5 V | Safe with or without the board regulator; required for C's jumper |
| Programs | Two commands sharing a library | One command with subcommands | Matches the two programs you asked for |
| Acquisition | Polling with absolute deadlines | FIFO | Enough for 200 Hz or less; FIFO is the high-rate option |
| Rate too high | Refuse with a fix | Silent step-down | Nothing hidden from the student |
| Data path | Raw integers live, Excel CSV built at stop | Write the Excel CSV live | Faster sampling loop; any run can be rebuilt |
| Gyro bias | From each trial's countdown | A daily calibration prompt | Bias drifts; the countdown costs no extra time |
| Accel calibration | 6 positions, once per module, before gluing | 1 or 3 placements | Gives the X/Y scale, which the Top ÷ Base ratio depends on |
| Naming | Tilt one sensor | Shake detection; typing bus addresses | Gentle on wires, no tuning, no addresses |
| CSV layout | Wide, one header row, fixed A/B/C columns | Long format; metadata above the header | Matches how Excel charts; trials line up |
| Faults | Blank cells plus a flag | Zeros, or dropping rows | A fault can't look like real data |
| UI | Plain ANSI terminal | `rich` | One fewer dependency |

## 15. Later / optional

- High-rate FIFO mode, unless Q2 moves it into v1.
- m/s² columns and gravity-removed columns.
- A TCA9548A multiplexer for 4 or more sensors. This only changes config: set `base=` on the mux overlay so its bus numbers stay fixed.
- One reader thread per bus, if the bench shows the bit-banged bus limits 3-sensor rates.
- A motion-triggered start with a pre-trigger buffer, for drop experiments.
- A helper that puts trials 1–3 of a condition on one chart.

## 16. Still to verify on the real boards

- What chip is actually on your three modules (WHO_AM_I), and how the 0x1D accel filter behaves on 6500/ICM dies.
- Each board's SDA/SCL pull-up value: measure from SDA to SCL, which reads about 2× one pull-up. Also check that each board has its 3.3 V regulator.
- Real per-read times on your Pi, which the bench reports.
- How long a failed transfer really takes on each bus with `I2C_RETRIES=0`. Pull Top's VCC mid-run and look at Base's row count.
