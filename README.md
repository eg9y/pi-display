# Pi Agent Hardware Monitor

A physical desk display that shows your [pi](https://pi.dev) coding agent's token usage, model, cost, and status in real time. Multi-slot support lets you monitor several agents at once — each on its own OLED tile.

The old Claude Code version required a hacky reverse proxy and hook scripts. Pi stores sessions as documented JSONL files you can tail directly.

## What You See on the Display

| Tile (OLED) | Shows |
|---|---|
| Top bar | Status icon + spinning loader (thinking/ tool use), session timer |
| Divider | Sweeping comet animation while active |
| Row 2 | `IN:` tokens (input + cache read + cache write) |
| Row 3 | `OUT:` tokens (cumulative output) |
| Row 4 | `$cost` (running total), `model` name |

All transitions: `idle` → `thinking` → `tool_use` → `done` → (30s) → `idle`

### Animations

- **Session done**: Clawd mascot bounces across the tile with a "CLAWD DONE!" banner and a chime
- **Waiting for user**: Gentle Clawd walk cycle with "YOUR TURN!" banner
- **Focus selected**: Big `K<slot>` badge drops in, sparkle corners, border pulse

## Hardware

| Item | Example | ~Cost |
|---|---|---|
| ESP32-WROOM-32 dev board | any devkit v1 clone | $8 |
| 2× 0.96" OLED SSD1306 (I2C, 128×64) | Amazon/AliExpress multipack | $10 |
| TCA9548A I2C multiplexer | also on Amazon/AliExpress | $4 |
| Half-size breadboard | 400 tie-point | $3 |
| Jumper wires (male-to-male) | assorted pack | $4 |
| Piezo buzzer (passive) | for chimes | $2 |
| (Optional) 12-LED WS2812B NeoPixel ring | status ring | $5 |
| (Optional) 470Ω resistor | series with NeoPixel DIN | $0.10 |

**Total without NeoPixel: ~$25**

### TCA9548A wiring (2 tiles → channels 6 & 7)

```
TCA    → ESP32
SDA  → GPIO 21
SCL  → GPIO 22
VIN  → 3.3V
GND  → GND

OLED holes on multiplexer channels 6 & 7:
  VCC → TCA VIN (3.3V rail)
  GND → TCA GND
  SDA → TCA SD0 (channel bus)
  SCL → TCA SC0 (channel bus)

Buttons:
  GPIO 4 → ch6 tile (slot 1)
  GPIO 5 → ch7 tile (slot 2)

Buzzer:
  GPIO 23 → passive buzzer + (the other leg to GND)
```

## Arduino Libraries

Install from **Sketch → Include Library → Manage Libraries**:

| Library | Author |
|---|---|
| Adafruit SSD1306 | Adafruit |
| Adafruit GFX Library | Adafruit |
| ArduinoJson | Benoît Blanchon |
| WebSockets | Markus Sattler |
| Adafruit NeoPixel | Adafruit *(optional)* |

Board: **Tools → Board → ESP32 Dev Module**

Upload `sketch_apr11a/sketch_apr11a.ino`, set WiFi credentials in the `CONFIG` block.

## Python Bridge

Install dependency:

```bash
pip install websocket-client
```

Run the bridge (pointed at the ESP32 IP shown on boot):

```bash
# Single agent (slot 1), most recent pi session anywhere
python pi_agent_bridge.py --ip 192.168.1.42

# Restrict to one project directory
python pi_agent_bridge.py --ip 192.168.1.42 --project ~/Projects/myapp

# Multi-slot — map keys K1/K2 to tmux sessions "agent-1" and "agent-2"
python pi_agent_bridge.py --ip 192.168.1.42 --sessions agent-1,agent-2

# Demo mode (no pi session needed)
python pi_agent_bridge.py --ip 192.168.1.42 --demo
```

The buttons on tile *N* send a `"focus"` event to the bridge; the bridge switches tmux to the matching session and raises the terminal window.

## How It Works

Pi saves every session as JSONL to:

```
~/.pi/agent/sessions/--Users-you-Projects-foo--/<timestamp>_<uuid>.jsonl
```

Each line is a typed entry documented in the [pi session format](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/session-format.md):

- `message` entries with `role: "user"` → "thinking"
- `message` entries with `role: "assistant"` → parse `usage.input`, `usage.output`, `usage.cost.total`, `stopReason`, extract tool calls
- `message` entries with `role: "toolResult"` → back to "thinking"
- `model_change` entries → update the displayed model name

No reverse proxy. No anthropic base URL hijacking. No intercepting SSE streams. Just a `tail -f` on a public file format.

## File Layout

| File | What it runs on | Purpose |
|---|---|---|
| `sketch_apr11a/sketch_apr11a.ino` | ESP32 | OLED tiles, WebSocket server, animations, beeper |
| `pi_agent_bridge.py` | Your computer | Tails pi JSONL sessions, forwards to ESP32 |

## License

MIT
