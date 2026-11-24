# Accelerometer logger (Raspberry Pi + GY-521 / MPU-6050)

Two command-line programs for a shake-table earthquake experiment:

- **`accel-setup`**: finds the sensors, shows live readings, lets you name them (for example `Base` and `Top`), and calibrates them.
- **`accel-record`**: records one trial at a time to a CSV file that opens straight in Excel.

Each trial runs like this:
1. Press Enter.
2. **HOLD STILL** for 3 s.
3. **GO**: pull the table, release it, and press **Space** as you let go.
4. Press **S** to stop.

The program then prints the peak shaking at the base and at the top, and the **amplification** (Top ÷ Base).

---

## 1. Wiring (Pi off!)

Solder the header pins onto each GY-521 first. Loose pins pushed into the holes make contact on and off.

On the module the pins are labelled **VCC GND SCL SDA XDA XCL AD0 INT**. Note that SCL comes before SDA on the module, but on the Pi SDA (pin 3) comes before SCL (pin 5).

| Sensor | Port | VCC → Pi pin | GND → | SDA → | SCL → | AD0 |
|---|---|---|---|---|---|---|
| **Base** (brick snapped onto the table) | A | 1 (3.3 V) | 6 | 3 | 5 | leave unconnected |
| **Top** (brick at the top of the tower) | B | 17 (3.3 V) | 20 | 16 | 18 | leave unconnected |
| (optional 3rd) | C | 1, shared with A | 9 | 3, shared | 5, shared | short wire on the module from VCC to AD0 |

Pin 1 has the square solder pad, at the end of the header farthest from the USB ports.

- **Power:** always 3.3 V (pins 1 and 17). **Never use 5 V.**
- **Unused pins:** leave XDA, XCL and INT unconnected.
- **Mounting on Duplo bricks**, so a sensor can come off for calibration:
  - Glue each module to its own Duplo brick, flat against one of the brick's smooth sides and square with its edges. Hot glue or thin double-sided foam tape both work.
  - Snap the Base brick onto the table's Duplo base. If the plexiglass has no Duplo on it, glue a Duplo plate to it first. Snap the Top brick onto the top of the tower.
  - Snap each brick on **the same way round every time**. A sticker arrow on the brick pointing in the pull direction helps.
  - If you can, have one of the board's printed arrows (X or Y) point in the pull direction. Then that axis's column is the shaking direction. `horiz_g` works whichever way the sensor faces.
  - Push each brick on fully and wiggle it: it must not rock. A loose brick rattles and adds fake spikes.
  - Keep each brick's cable plugged into the same Pi pins (Base on port A, Top on port B). Calibration is stored per port, so it only stays right if each sensor stays on its own port.
- **Cables:**
  - Keep them to 30 cm or less.
  - Leave a slack loop so the table and tower can move freely.
  - Tape the plugs at the Pi end. If the tower falls, it must not yank the wires.
- **Labels:** label each module and both ends of its cable (A – Base, B – Top).

## 2. Install on the Pi (once, at home, with internet)

From your computer, copy this folder to the Pi. Replace `you@raspberrypi.local` with your Pi's user and hostname:
```bash
scp -rp accel you@raspberrypi.local:~/
```
Then, on the Pi over SSH:
```bash
sudo bash ~/accel/install.sh
```
The installer:
- installs `python3-smbus2`, `i2c-tools` and `tmux`
- turns on I2C and adds a second I2C bus (bus 3 on GPIO 23/24) to `/boot/firmware/config.txt`
- adds you to the `i2c` group
- creates `~/accel-data`
- adds the two commands

Reboot when it asks. Afterwards, check the buses:
```bash
ls /dev/i2c-*          # should include /dev/i2c-1 and /dev/i2c-3
i2cdetect -y -r 1      # should show 68 (Base)
i2cdetect -y -r 3      # should show 68 (Top)
```

## 3. `accel-setup`: name and calibrate

```bash
accel-setup
```
- **Live table:** one row per sensor. A sensor lying flat should show **total g ≈ 1.00**.
- **[N] Name:** pull one sensor's brick off and turn it over, and the program spots which one moved (or press its port letter). Then type a name such as `Base` or `Top`. At the name prompt, Enter = no change and `-` = remove the name.
- **[C] Calibrate (6 faces, about 2 minutes per sensor):**
  - Pull the sensor's brick off.
  - Rest the brick still on each of its 6 faces on a level table, in any order. Studs-down counts as a face.
  - For the face the sensor is glued to, press that face flat against the side of a box. Or rest the brick across a gap so the pins and plugs hang free.
  - A face doesn't have to be perfectly straight: the program combines all six readings, so a few degrees off doesn't matter.
  - A tick appears for each face once it has been held still for 2 seconds.
  - It corrects each axis's offset and scale, which makes the Top ÷ Base ratio more accurate.
  - **Do it once per sensor.** Redo it only if you re-glue a sensor or swap modules. Each trial's HOLD STILL countdown re-measures the gyro, so there is nothing to redo daily.
- **[S] Settings:** samples per second (default 100), accel range (default ±8 g), gyro range (default ±1000 °/s).
- **[R] Rescan** after fixing a wire. **[Q] Quit.** Everything saves automatically.

If a sensor shows **CHECK** or **not found**, the screen says what to look at.

## 4. `accel-record`: run trials

```bash
accel-record
```
- **On SSH:** it automatically restarts itself inside `tmux`, so a dropped Wi-Fi connection can't stop a recording. If you get disconnected, log back in and type `tmux attach -t accel`.
- **It asks for:**
  - a **condition label**, such as `h8_plain` or `h8_braced` (letters, digits and `_`)
  - the **trial number** (filled in for you)
  - the **duration** (0 = until you press S)
  - an optional **note**
- **Ready screen:** shows a motion level for each sensor. Gently tap the **top of the tower**: Top's motion should jump much more than Base's. If Base jumps more, the sensors or cables are swapped.
- **Enter**, then **HOLD STILL 3-2-1** (hands off; this measures each sensor at rest), then **GO**.
- **Pull and release the table**, pressing **Space** as you let go, then **S** to stop.
- **The summary** shows:
  - peak and RMS sideways shaking for Base and Top
  - **Top / Base peak ratio (amplification)**. It always divides by the sensor *named* Base, whichever port it's on.
  - notes such as "Top tilted or fell at 4.12 s"
  - a verdict: **GOOD** or **CHECK**, with the reason
- **Next:** **Enter** starts the next trial of the same condition. **L** starts a new label. **B** marks the last trial bad (the file is kept, and `runs.csv` says BAD). **Q** quits.

Do several trials of each condition (for example 5 × `h8_plain`, 5 × `h8_braced`) and compare.

## 5. Your files (`~/accel-data`)

| File | What it is |
|---|---|
| `h8_plain_t01_20261123-1405.csv` | **One trial, for Excel.** One row every 0.01 s. |
| `runs.csv` | **One row per trial**: label, trial, note, `amplification`, peaks, RMS, when a sensor fell, quality. Good for a results table. |
| `details/…_raw.csv` | Raw sensor numbers (kept so anything can be recomputed). |
| `details/…_info.json` | Everything about the run: settings, chip types, timing, errors. |
| `sensors.json` | Names, settings and calibration. |

**Columns in a trial CSV:**

| Column | Meaning |
|---|---|
| `time_s` | Seconds since GO |
| `Base_ax_g`, `Base_ay_g`, `Base_az_g` | Acceleration on each axis, in g (1 g = 9.81 m/s²) |
| `Base_total_g` | Total acceleration. It is 1.00 at rest, because gravity is always included. |
| `Base_horiz_g` | **Sideways shaking, with gravity removed.** This is the main earthquake number. It doesn't depend on which way the sensor is glued. |
| `Base_gx_dps` … `Base_gz_dps` | Rotation speed in degrees per second (the tower rocking) |
| `mark` | Counts your Space presses (a step at the moment you released the table) |
| `flags` | Usually empty. Otherwise one or more of the flags below. |

**Flags:**
- `ERR:Top`: no data for that sensor in that row. The cells are left blank.
- `CLIP:Top`: acceleration hit the top of its range.
- `GCLIP:Top`: rotation hit the top of the gyro range. The sideways numbers are fine.
- `TILT:Top`: the sensor has tilted or fallen over since HOLD STILL, so `horiz_g` from here on includes gravity.
- `LATE`: that sample was taken late.

**Copy the files to your computer** (run this on the computer, not the Pi):
```bash
scp -r you@raspberrypi.local:accel-data ~/Desktop/
```

## 6. Charting in Excel

1. Open a trial CSV.
2. Select the `time_s` column, then hold Ctrl (⌘ on a Mac) and select `Base_horiz_g` and `Top_horiz_g`.
3. Insert → **Scatter with Straight Lines**. Don't use a Line chart: it spaces points evenly and ignores the real times.

To compare conditions, open `runs.csv`, which has one row per trial. The `amplification` column is Top peak ÷ Base peak. The per-sensor columns are `A_peak_horiz_g` (port A = Base), `B_peak_horiz_g` (port B = Top) and `A_rms_horiz_g` / `B_rms_horiz_g`. Filter out rows whose `quality` is BAD.

## 7. What the numbers mean

- **Gravity is always included.** An accelerometer at rest reads 1 g straight up. That's why `total_g` ≈ 1.00 when nothing moves, and why the earthquake column `horiz_g` subtracts the HOLD STILL reading first.
- **Peak horizontal acceleration** is how hard the "ground" (Base) or the building (Top) was shaken at the worst moment. Seismologists call the ground's version **PGA**.
- **RMS** is the average strength of the shaking over the **5 seconds after you press Space** at the release (or after Base starts shaking, if you forgot Space). That makes trials comparable however long you kept recording. Keep recording at least 5 s after the release. Otherwise the summary shows the RMS as "(short)" and `runs.csv` leaves it blank.
- **Amplification = Top peak ÷ Base peak.** If Base barely moved (under 0.05 g), there's no ratio and the verdict says CHECK. If the tower falls before its shaking can be measured, there's no ratio for that trial either; the summary says so.
- **If the tower ends up leaning** 5° or more, a note warns that part of `horiz_g` after that is gravity, not shaking. If it falls over (more than 15°), the rows after the fall are flagged `TILT` and left out.
  - More than 1: the tower makes the shaking worse at the top.
  - Less than 1: the tower absorbs some of it.
- **Timing:** both sensors are read within about 2 ms of each other, 100 times a second.
- **Filtering:** each sensor's built-in filter smooths out anything faster than about 20 Hz. That's fine for a shake table, which moves at a few Hz.

## 8. Experiment-day checklist

1. Wires taped, cables have slack, and sensor bricks pushed on fully, the same way round as always, with no rocking.
2. `accel-setup`: both sensors show **OK**. Quit.
3. `accel-record`, then enter the label and note. On the ready screen, tap the top of the tower: Top's motion should jump more than Base's.
4. HOLD STILL, GO, pull and release (Space), wait for the tower to settle, then S.
5. Read the verdict. If it says CHECK, fix the cause and redo the trial.
6. At the end of the day, copy `~/accel-data` to your computer.

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `not found` for Base | Check pins 1, 6, 3, 5 (SDA and SCL swapped?), then **[R]escan**. |
| `not found` for Top | Check pins 17, 20, 16, 18, and that `ls /dev/i2c-3` exists (run install.sh, then reboot). |
| `No permission to use /dev/i2c-1` | Log out and back in (or reboot). Don't use sudo. |
| `taken by kernel driver` | Remove that `dtoverlay=` line from `/boot/firmware/config.txt`, then reboot. |
| `CHECK: reads 0.7 g at rest` | Bad module: try another one. |
| `too slow for 100 samples/s` | Use fewer sensors or a lower rate (accel-setup → [S]). |
| `CLIP:` flags | Raise the accel range in accel-setup → [S] (for example ±16 g). |
| `GCLIP:` flags | The tower spun fast, probably while falling. Ignore it, or raise the gyro range if you need rotation data. |
| `moved during HOLD STILL` | Keep hands off during the countdown. The run is still saved. |
| `... is already running` | Only one program can use the sensors at a time. If it's accel-record left open after your SSH connection dropped, run `tmux attach -t accel` and quit it with Q. |
| `sudo: ... install.sh: command not found` | Use `sudo bash ~/accel/install.sh` (with `bash`). |
| Power cut during a run | The next `accel-record` rebuilds the cut-off trial and marks it `interrupted`. |

## 10. Trying it without hardware

Both programs have a simulator with a shake table, where the "tower top" amplifies about 1.9×:
```bash
python3 accel_setup.py --sim
python3 accel_record.py --sim               # after GO, press Space to start a simulated shake
python3 accel_record.py --sim-faults --label test --duration 9   # with injected wiring faults
```
Set `ACCEL_DATA_DIR=/tmp/accel-test` to keep test data separate from real data.

Run the automated tests from this folder:
```bash
python3 -m unittest discover -s tests -t .
```
