# Claude Orchestration Console — Hardware Guide

A build guide for turning `claude-display` from a single-tile status OLED into a multi-agent **orchestration console**: N small OLEDs (one per Claude Code agent) + mechanical keys that focus the matching terminal on your Mac.

The value prop shifts from "pretty status display" to **physical control surface for juggling parallel agents**.

---

## Build status — 2026-05-07

**Working on the bench:**

- 2× SSD1306 OLEDs on TCA9548A channels 7 and 6, rendering live data
- Buzzer on D23 (boot jingle plays)
- 4× Kailh Choc White switches soldered to perfboard:
  - K1 → D4 (active in firmware, focus event confirmed via Serial)
  - K2 → D5 (active in firmware, focus event confirmed via Serial)
  - K3 → D18 (wired, firmware not polling yet)
  - K4 → D19 (wired, firmware not polling yet)
- GND rail: single tinned bare wire across all 4 switch GND pins, exits to ESP32 GND via one M Dupont
- `claude_monitor_bridge.py` already has the inbound WS listener (`_listen_loop`) + `tmux switch-client` dispatch (`focus_agent`). No Python changes needed for the next milestone.

**Immediate next steps — focus dispatch end-to-end:**

1. Update WiFi credentials in `sketch_apr11a/sketch_apr11a.ino:18-19` (hardcoded SSID currently fails; bridge can't reach the ESP32 without WiFi). Re-flash and capture the IP printed at boot.
2. Install tmux if needed (`brew install tmux`). Create a session with windows named `agent-1` and `agent-2` — running `bash` in each is fine for the test, only the names matter.
3. Run the bridge: `python claude_monitor_bridge.py --ip <ESP32_IP> --terminal <Ghostty|iTerm|Terminal|Alacritty>`.
4. Press K1 → tmux switches to `agent-1` and terminal raises. K2 → `agent-2`. That's the demo moment.

**Optional next — activate K3 and K4 in firmware:**

- Bump `BUTTON_PINS[] = {4, 5, 18, 19}` (sketch_apr11a/sketch_apr11a.ino:54)
- Drop the `static_assert` tying buttons to `NUM_TILES` (sketch_apr11a/sketch_apr11a.ino:55-56) and add a `NUM_BUTTONS` constant for sizing the debounce arrays
- Update both `setupButtons` and `pollButtons` loops to iterate `NUM_BUTTONS` instead of `NUM_TILES`

**After that:** OLEDs 3 and 4 on TCA channels 5 and 4 → NeoPixel ring on D13 → scale to 8 → enclosure.

---

## Architecture

```
┌──────────────┐     WS      ┌──────────────┐   I2C    ┌───────────────┐
│   Mac host   │◄───────────►│ ESP32 WROOM  │─────────►│ TCA9548A mux  │
│              │             │              │          └───────┬───────┘
│ bridge.py    │             │   firmware   │                  │ (8 ch)
│  + focus     │             │              │          ┌───────┴───────┐
│    dispatch  │             │              │          │ 8× SSD1306    │
│ tmux         │             │              │          │   OLEDs       │
└──────────────┘             └──────┬───────┘          └───────────────┘
                                    │
                              ┌─────┴──────┐
                              │ 8× Kailh   │
                              │ Choc keys  │
                              └────────────┘
```

**Data flow**

1. `claude_monitor_bridge.py` watches the parent Claude Code session JSONL. When an assistant message contains a `tool_use` block with `name: "Task"`, it detects the subagent and starts tailing its child session JSONL. Each agent is assigned a slot `1..N`.
2. Bridge sends per-agent payloads over one WebSocket:
   `{slot: 3, agent: "agent-3", status: "tool_use", tokens_in: ..., cost: ..., tool_name: "Bash"}`
3. ESP32 routes each payload to its tile via the TCA9548A mux and renders.
4. Key press on slot 3 → ESP32 sends `{event: "focus", slot: 3}` back over the same WS.
5. Bridge receives the focus event and runs `tmux switch-client -t agent-3` (plus an `osascript activate` to raise the terminal).

**Assumption:** agents run inside `tmux` windows with predictable names (`agent-1` … `agent-8`). This is by far the simplest focus path — one line per button, no window-ID tracking, survives app restarts.

---

## Bill of Materials

8-tile build, ~$35 since the ESP32 is owned. A 4-tile v1 runs ~$18.

| Part | Qty | Unit | Notes |
|---|---|---|---|
| ESP32 DevKit (WROOM-32) | 1 | owned | Classic ESP32 is plenty for this build. See pin plan below. |
| SSD1306 0.96" 128×64 **I2C** OLED | 8 | ~$2.50 | Confirm 4-pin I2C module, not SPI. |
| TCA9548A I2C multiplexer breakout | 1 | ~$2 | 8 isolated I2C channels, one per OLED. Default address 0x70. |
| Kailh Choc V1 low-profile switches | 8 | ~$0.50 | **Recommended button.** See button section. |
| Blank MBK / Choc 1u keycaps | 8 | ~$0.50 | Flat, labelable. |
| Kailh Choc hot-swap sockets (optional) | 8 | ~$0.30 | Skip if soldering direct. |
| Passive piezo buzzer (3–5V) | 1 | ~$1 | Single GPIO + `tone()`. |
| WS2812 NeoPixel ring (optional) | 1 | ~$3 | Already stubbed in `sketch_apr11a.ino`. |
| Perfboard or half-size breadboard | 1 | ~$3 | |
| Dupont jumper wires (M-M, M-F) | 1 set | ~$3 | |

### Why TCA9548A and not SPI displays

SSD1306 I2C has only two usable addresses (0x3C / 0x3D), so you cap at 2 without help. SPI SSD1306 variants work but need shared SCK/MOSI/DC/RST **plus a CS line per display** — more wiring, more soldering, more footprint. The TCA9548A is one breakout, one byte write to select a channel, and you keep using vanilla `Adafruit_SSD1306` unchanged.

### Classic ESP32 (WROOM-32) is plenty

- 8× SSD1306 framebuffers total ~8 KB of SRAM — nothing for a 520 KB chip.
- Hardware I2C on GPIO 21/22 drives the TCA9548A; all 8 tiles share that one bus.
- 34 usable GPIOs: 8 buttons + buzzer + NeoPixel still leaves headroom.
- `Adafruit_SSD1306`, `Adafruit_NeoPixel`, `ArduinoJson`, `WebSocketsServer` (all already in the sketch) run identically.
- Trade-offs vs ESP32-S3: no native USB (flash via CP2102, no practical difference), less headroom if you later add WAV audio via I2S. Neither matters for v1.

---

## Pin plan (WROOM-32)

Avoiding strapping pins (0, 2, 12, 15), input-only pins (34–39), and flash pins (6–11):

| Function | Pin(s) |
|---|---|
| I2C SDA / SCL → TCA9548A → 8× OLED | 21 / 22 |
| Buttons 1–8 (`INPUT_PULLUP`, to GND) | 4, 5, 16, 17, 18, 19, 25, 26 |
| Passive buzzer (`tone()`) | 23 |
| NeoPixel data (already in sketch) | 13 |
| Spare / future | 14, 27, 32, 33 |

All 8 button pins support internal pull-ups and have no boot conflicts. No GPIO matrix — each key gets its own dedicated line, which kills any ghosting and simplifies debounce.

> **30-pin board caveat:** the smaller ESP32 dev board variants don't break out **GPIO 16 and 17** (used internally on some chip revs). On a 30-pin board, swap those for `18, 19` (this build) or `14, 27` from the spare row. The 38-pin variant exposes 16/17 normally.

---

## Button choice — why mechanical keys

The tactile interaction *is* the viral angle. A mushy through-hole button kills the demo. Options, ranked:

### Recommended: Kailh Choc V1 (low-profile)

- 11.5 mm tall — shallow enough for a thin 3D-printed enclosure.
- Feels: clicky (**Choc White**), tactile (**Choc Sunset** / Brown), linear (**Choc Red**).
- **Sunset is the crowd favorite**: satisfying tactile bump, quiet enough for an office, great on video.
- Huge keycap ecosystem (MBK, Chocfox) — blank 1u caps you can label `A1`…`A8`.
- Plate cutout: 13.8 × 13.8 mm, 1.2 mm plate thickness. Solders direct to perfboard, or use hot-swap sockets.
- ~$0.40–0.60 each in packs of 10.

**v1 pick: 8× Kailh Choc Sunset + blank MBK keycaps.** Swap to Choc White if you specifically want click-clack audio on camera.

### Alternatives

- **Cherry MX / Gateron (full-size).** Taller (~18 mm), bulkier, iconic look, massive keycap selection. Gateron Browns or Kailh Box Whites are the obvious picks. Pick these if you want the enclosure to read as "tiny keyboard."
- **6×6 mm through-hole tactile.** $0.05 each, electrically fine, but mushy. Skip.
- **30 mm arcade buttons.** Cartoonishly satisfying but ~35 mm² of panel space each — 8 of them is a shoebox. Fun for a 2–4 tile build, too big for 8.

### Wiring the keys

Dedicated GPIO per switch: `GPIO → switch → GND`, with `pinMode(pin, INPUT_PULLUP)` and 10 ms software debounce. No matrix needed at this count.

---

## Firmware changes

File: `sketch_apr11a/sketch_apr11a.ino`

1. **TCA9548A channel select helper**
   ```cpp
   void tca_select(uint8_t ch) {
     Wire.beginTransmission(0x70);
     Wire.write(1 << ch);
     Wire.endTransmission();
   }
   ```
2. **One shared `Adafruit_SSD1306` instance** across tiles, calling `tca_select(slot)` before each `clearDisplay()` / `display()` pair. This works because selecting a channel reroutes the I2C target — the same display object talks to whichever tile is currently active.
3. **Per-slot state** — `TileState tiles[NUM_TILES]` holding status, tokens, cost, model, todos, tool name, last-update ms. Reuse the existing render function per slot.
4. **WS payload routing** — extend the `ArduinoJson` parser to read a `slot` field and update the right tile. Keep slot-less payloads as `slot: 0` for back-compat with the existing demo mode.
5. **Button scan loop** — poll 8 GPIOs every 5 ms, debounce, emit `{event:"focus", slot:N}` on the falling edge via `webSocket.broadcastTXT(...)`.
6. **Buzzer jingles** — add `BUZZER_PIN 23`, write a tiny `playJingle(kind)` using `tone()` / `noTone()`. Fire on inbound `session_done` and `waiting_for_user` events (already emitted by the bridge).
7. **NeoPixel activation** — uncomment the existing `USE_NEOPIXEL` block (pin 13). Map ring LEDs to tiles as a secondary visual cue (cost meter, active-tile highlight, or per-agent status color).

---

## Bridge changes

File: `claude_monitor_bridge.py`

Current bridge follows one session. For N agents we need multi-session tailing with slot assignment.

1. **Detect subagent spawns.** In `apply_line`, when an assistant message contains a `tool_use` block with `name == "Task"`, the child session JSONL appears moments later under `~/.claude/projects/<same-encoded-cwd>/<new-uuid>.jsonl`. Associate it with a free slot.
2. **`SlotManager`** — dataclass holding `{slot: SessionState}` with `assign(session_path) -> slot` and `release(slot)`. Reuse the existing `SessionState`, `apply_line`, `refresh_todos`, pricing helpers — none of that logic needs to change, it just runs per-slot.
3. **Parallel tailers.** Convert `tail_jsonl` to run one thread per active session file, all pushing into a shared `queue.Queue` that the WS sender drains. Threads keep the diff small; asyncio is overkill here.
4. **Payload shape.** Every `ws_client.send(...)` now includes `"slot": n`. Add events:
   - `{"event":"agent_spawned","slot":3,"name":"agent-3"}`
   - `{"event":"agent_done","slot":3}`
5. **Inbound messages.** Open the WS for reads. On `{event:"focus", slot:N}`, dispatch to `focus_agent(slot)` (below).
6. **Demo mode.** Update `--demo` to cycle through 4 synthetic slots so the hardware is testable without running real agents.

---

## Focus dispatch (inline in bridge)

```python
import subprocess

TERMINAL_APP = "Ghostty"  # or "iTerm", "Terminal", "Alacritty"

def focus_agent(slot: int):
    name = f"agent-{slot}"
    subprocess.run(["tmux", "switch-client", "-t", name], check=False)
    subprocess.run(
        ["osascript", "-e", f'tell application "{TERMINAL_APP}" to activate'],
        check=False,
    )
```

**Agent launching convention** (set up once):

```bash
tmux new-session -d -s claude
tmux new-window -t claude -n agent-1 'cd ~/Projects/foo && claude'
tmux new-window -t claude -n agent-2 'cd ~/Projects/bar && claude'
tmux new-window -t claude -n agent-3 'cd ~/Projects/baz && claude'
```

Then `tmux switch-client -t agent-N` snaps focus instantly.

---

## Build order

Work incrementally so each step is demo-able on its own:

1. ✅ **Single-tile buzzer + jingle.** On the existing breadboard. Biggest sensory payoff per wire added; confirms the `session_done` and `waiting_for_user` event hooks.
2. ✅ **Two-tile prototype.** Add TCA9548A + a second OLED. Prove the channel-select pattern and the per-slot payload protocol by rendering the same data on both tiles. *(Currently rendering on TCA channels 7 and 6.)*
3. ⏸ **Bridge multi-session support.** Implement `SlotManager` and threaded tailing. Test with `--demo` driving 4 synthetic slots. *(Bridge has the focus-event listener and dispatch already; multi-session SlotManager + parallel tailing not yet implemented — still single-session.)*
4. ⏳ **4 Kailh Choc switches on perfboard.** Implement button scan + focus event. Set up the tmux convention. Target demo: press a key, terminal focuses the right window. *(Switches soldered + button scan working for K1/K2; WS focus event confirmed in Serial. Pending tmux setup + WiFi fix to verify focus dispatch end-to-end.)*
5. **Scale to 8 tiles + 8 keys.** Lock down pin assignments from the table above.
6. **NeoPixel ring.** Secondary visual — cost meter, active-tile highlight, or per-agent status color.
7. **3D-printed enclosure.** Now that the electronics are locked, design around:
   - Choc plate cutouts: 13.8 × 13.8 mm, 1.2 mm plate
   - OLED windows: ~25 × 14 mm visible area, offset for the module PCB
   - USB passthrough + buzzer grill
8. **Launch content.** 15-second video: 6 tiles thinking, mash key 3, terminal snaps to agent-3, buzzer ding. Ship on r/esp32, Show HN, X.

---

## Verification

**Bench tests (no real agents):**

- `python claude_monitor_bridge.py --ip <esp32> --demo` drives 4 synthetic slots. Each tile updates independently, status cycles `idle → thinking → tool_use → done`, buzzer fires on `done`.
- Press each physical key → serial monitor prints `focus event slot=N` and the host receives a focus message.
- Power-cycle the ESP32 mid-session → bridge reconnects (existing `WSClient.connect` backoff handles this).

**Live orchestration test:**

- Launch 3 tmux windows `agent-1/2/3`, each running `claude` in a different project.
- Start the bridge (no `--demo`). Tiles 1–3 should show real model, tokens, cost.
- In agent-1, have Claude spawn a subagent (Task tool). Slot 4 should auto-assign and start rendering.
- Press key 2 → host terminal focuses `agent-2` window within ~100 ms.
- Run an expensive tool call in agent-3 → tile 3 ticks cost, buzzer fires on `end_turn`, NeoPixel flashes on slot 3.

**Failure modes to check:**

- Two subagents spawn in the same second — `SlotManager` must not double-assign.
- An agent exits — its tile clears and its slot becomes reusable.
- All 8 tiles busy and a 9th agent spawns — bridge logs an overflow warning, does not crash.
- Dead I2C channel — `tca_select` on a hung channel shouldn't block the others. Add a short `Wire.setTimeOut(...)`.

---

## Build lessons (learned the hard way)

### Clipping the bottom plastics: the slider tip also protrudes

A Kailh Choc V1's bottom has 5 plastic protrusions: 2 outer alignment pegs, 1 large center alignment post, and — the one that's easy to miss — **the bottom tip of the slider itself**, which extends below the housing when the switch is fully pressed. Clipping the 3 alignment plastics flush is fine, but if you don't also handle the slider tip, the slider hits the perfboard surface mid-travel and the switch can't reach actuation. Symptom: press feels muffled, short travel, no click, contacts never close even though all wiring tests good.

**Procedure that works:**

1. Clip the 2 outer alignment pegs flush with the housing.
2. Clip the big center alignment post flush.
3. **Press the white stem fully** with the switch upside-down on the bench. The slider's bottom tip will pop out through the center hole on the underside of the housing.
4. Holding the press, **clip that protruding slider tip flush** with flush cutters.
5. Release the stem — the slider retracts; what's left is a switch whose bottom is fully flat against any surface and whose internal travel is unobstructed.

Now the switch can mount flush on a flat perfboard and still actuate fully (the click + full ~3mm travel returns).

### 30-pin ESP32 boards skip GPIO 16/17

The compact 30-pin dev boards don't break out GPIO 16/17. Don't waste time looking for them — pick from the spares column or use 18/19 instead. Documented in the pin plan caveat above.

### Diagnostic ladder for "button doesn't trigger"

When a soldered switch produces no Serial output on press, work down this list before suspecting the firmware. Saved hours on this build:

1. **Bridge D4 directly to GND** with a spare jumper (no perfboard, no switch). If `slot=1` prints, the ESP32 + breadboard rows are fine — the issue is on the perfboard side.
2. **Touch a jumper from ESP32 GND to the switch's GND-side metal pin** on the bottom of the perfboard. If `slot=1` prints, GND rail solder is fine; problem is the GPIO joint or the switch itself.
3. **Touch a jumper from ESP32 GND to the switch's GPIO-side metal pin.** If `slot=1` prints, both wires are fine; the switch itself isn't actuating mechanically.
4. **Compare to a fresh unsoldered switch.** Click both stems side-by-side. The mounted one feeling muffled / short-travel / silent vs. the loose one clicking cleanly = the slider tip is hitting the perfboard. Apply the slider-tip clip procedure above.

---

## Open decisions

Still to lock in before wiring:

1. **tmux workflow** — already using it, or migrating as part of this project?
2. **Terminal app** — Ghostty / iTerm2 / Terminal.app / Alacritty? (Sets the `osascript activate` target.)
3. **v1 tile count** — 4 (fits a breadboard) or 8 (final form)? 4 is the cheaper, faster first milestone.
4. **Choc switch feel** — Sunset (tactile, quiet), White (clicky, loud — best on camera), or Red (linear, silent)?
5. **Color displays later?** — monochrome SSD1306 is fine for v1, but confirm ST7789 isn't on the roadmap so the mux pick stays compatible.
