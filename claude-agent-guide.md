# Claude Code Hardware Monitor — Complete Build Guide

> A physical desk display that shows your Claude Code token usage, agent status, and session stats in real time.

---

## Part 1: What You're Building

A small screen on your desk that shows:
- **Agent status** — idle, thinking, tool use, done
- **Token count** — input and output tokens for the current session
- **Estimated cost** — running dollar amount
- **Session timer** — how long the current task has been running
- **LED status ring** — pulses blue while thinking, green when done, red on error

The system has two pieces:
1. **ESP32 + OLED display** (the physical device)
2. **Bridge script** on your computer that watches Claude Code and sends data to the device over WiFi

---

## Part 2: Shopping List

### Required

| Item | Example Product | ~Cost |
|---|---|---|
| ESP32 dev board | ESP32-WROOM-32 DevKit v1 | $8 |
| 0.96" OLED display (I2C, SSD1306, 128x64) | Any SSD1306 module (4-pin I2C) | $5 |
| Breadboard (half-size is fine) | 400 tie-point breadboard | $3 |
| Jumper wires (male-to-male) | Assorted pack | $4 |
| Micro-USB cable | (you probably have one) | — |

### Optional (for the LED status ring)

| Item | Example Product | ~Cost |
|---|---|---|
| NeoPixel Ring (12 LEDs) | WS2812B 12-LED ring | $5 |
| 1× 470Ω resistor | — | $0.10 |

**Where to buy:** Amazon, Adafruit, SparkFun, AliExpress (cheaper but slower shipping).

**Total cost: ~$20–25**

---

## Part 3: Install Software

### 3a. Arduino IDE

1. Download **Arduino IDE 2.x** from https://www.arduino.cc/en/software
2. Install it. Open it.

### 3b. Add ESP32 board support

1. In Arduino IDE, go to **File → Preferences**
2. In "Additional Board Manager URLs", paste:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. Click OK
4. Go to **Tools → Board → Board Manager**
5. Search `esp32`, install **"esp32 by Espressif Systems"** (latest version)
6. Go to **Tools → Board** and select **"ESP32 Dev Module"**

### 3c. Install required libraries

In Arduino IDE, go to **Sketch → Include Library → Manage Libraries** and install:

| Library | Author | What it does |
|---|---|---|
| Adafruit SSD1306 | Adafruit | Drives the OLED screen |
| Adafruit GFX Library | Adafruit | Graphics primitives (text, shapes) |
| ArduinoJson | Benoît Blanchon | Parses JSON from the bridge script |
| Adafruit NeoPixel | Adafruit | (Optional) Controls the LED ring |
| WebSockets | Markus Sattler | WebSocket server on ESP32 |

### 3d. Install Python (for the bridge script)

You likely have Python 3 already. Verify:
```bash
python3 --version
```

Install the one dependency:
```bash
pip install websocket-client
```

---

## Part 4: Wiring

### OLED Display (4 wires)

```
OLED Pin    →    ESP32 Pin
────────────────────────────
VCC         →    3.3V
GND         →    GND
SDA         →    GPIO 21
SCL         →    GPIO 22
```

### NeoPixel Ring (optional, 3 wires)

```
NeoPixel    →    ESP32 Pin
────────────────────────────
VIN/5V      →    5V (VIN pin)
GND         →    GND
DIN         →    GPIO 13  (through 470Ω resistor)
```

### Wiring Diagram (text)

```
                    ┌──────────────────┐
                    │    ESP32 Board    │
                    │                  │
   OLED SDA ───────┤ GPIO 21     3.3V ├─────── OLED VCC
   OLED SCL ───────┤ GPIO 22      GND ├──┬──── OLED GND
                    │                  │  └──── NeoPixel GND
   NeoPixel DIN ─┬─┤ GPIO 13      5V  ├─────── NeoPixel VIN
             470Ω│  │                  │
                 └──┤                  │
                    └──────────────────┘
```

### Step-by-step wiring instructions

1. Place the ESP32 on the breadboard straddling the center groove
2. Place the OLED module on the breadboard near the ESP32
3. Connect OLED **VCC** → ESP32 **3.3V** pin (use a jumper wire)
4. Connect OLED **GND** → ESP32 **GND** pin
5. Connect OLED **SDA** → ESP32 **GPIO 21**
6. Connect OLED **SCL** → ESP32 **GPIO 22**
7. (Optional) Connect NeoPixel **VIN** → ESP32 **5V/VIN**
8. (Optional) Connect NeoPixel **GND** → ESP32 **GND**
9. (Optional) Place 470Ω resistor between ESP32 **GPIO 13** and NeoPixel **DIN**
10. Plug the ESP32 into your computer via USB

---

## Part 5: ESP32 Firmware

Create a new sketch in Arduino IDE and paste this entire code:

```cpp
// ============================================================
// Claude Code Hardware Monitor — ESP32 Firmware
// ============================================================

#include <WiFi.h>
#include <WebSocketsServer.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <ArduinoJson.h>

// ---- Optional: Uncomment if using NeoPixel ring ----
// #define USE_NEOPIXEL
// #include <Adafruit_NeoPixel.h>

// ===================== CONFIG =====================
// CHANGE THESE to your WiFi network name and password
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
// ==================================================

// OLED setup
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// WebSocket server on port 81
WebSocketsServer webSocket = WebSocketsServer(81);

// Optional NeoPixel
#ifdef USE_NEOPIXEL
#define NEO_PIN 13
#define NEO_COUNT 12
Adafruit_NeoPixel ring(NEO_COUNT, NEO_PIN, NEO_GRB + NEO_KHZ800);
#endif

// ---- State ----
String agentStatus   = "idle";
long   tokensIn      = 0;
long   tokensOut     = 0;
float  costUSD       = 0.0;
String modelName     = "—";
unsigned long sessionStartMs = 0;
bool   sessionActive = false;

// ---- Animation ----
unsigned long lastPulse = 0;
int pulseVal = 0;
int pulseDir = 5;

// ---- Helper: format large numbers ----
String formatTokens(long n) {
  if (n >= 1000000) return String(n / 1000000.0, 1) + "M";
  if (n >= 1000)    return String(n / 1000.0, 1) + "k";
  return String(n);
}

// ---- Helper: format elapsed time ----
String formatElapsed(unsigned long ms) {
  unsigned long s = ms / 1000;
  int h = s / 3600;
  int m = (s % 3600) / 60;
  int sec = s % 60;
  char buf[16];
  sprintf(buf, "%02d:%02d:%02d", h, m, sec);
  return String(buf);
}

// ---- Draw the screen ----
void drawDisplay() {
  display.clearDisplay();

  // Row 1: Status bar
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);

  // Status icon
  if (agentStatus == "thinking") {
    display.print("\x07 THINKING");  // bullet char
  } else if (agentStatus == "tool_use") {
    display.print("> TOOL USE");
  } else if (agentStatus == "done") {
    display.print("\x04 DONE");      // diamond
  } else if (agentStatus == "error") {
    display.print("! ERROR");
  } else {
    display.print("- IDLE");
  }

  // Session timer on the right
  if (sessionActive && sessionStartMs > 0) {
    String elapsed = formatElapsed(millis() - sessionStartMs);
    int16_t x1, y1;
    uint16_t w, h;
    display.getTextBounds(elapsed, 0, 0, &x1, &y1, &w, &h);
    display.setCursor(SCREEN_WIDTH - w, 0);
    display.print(elapsed);
  }

  // Divider line
  display.drawLine(0, 10, SCREEN_WIDTH, 10, SSD1306_WHITE);

  // Row 2: Token counts
  display.setCursor(0, 14);
  display.print("IN:");
  display.setCursor(20, 14);
  display.setTextSize(2);
  display.print(formatTokens(tokensIn));

  display.setTextSize(1);
  display.setCursor(0, 32);
  display.print("OUT:");
  display.setCursor(28, 32);
  display.setTextSize(2);
  display.print(formatTokens(tokensOut));

  // Row 3: Cost + Model
  display.setTextSize(1);
  display.setCursor(0, 52);
  display.print("$");
  display.print(String(costUSD, 4));

  // Model name on the right
  int16_t x1m, y1m;
  uint16_t wm, hm;
  display.getTextBounds(modelName, 0, 0, &x1m, &y1m, &wm, &hm);
  display.setCursor(SCREEN_WIDTH - wm, 52);
  display.print(modelName);

  display.display();
}

// ---- Update NeoPixel ring based on status ----
void updateLEDs() {
#ifdef USE_NEOPIXEL
  unsigned long now = millis();

  if (agentStatus == "thinking" || agentStatus == "tool_use") {
    // Pulsing blue
    if (now - lastPulse > 30) {
      lastPulse = now;
      pulseVal += pulseDir;
      if (pulseVal >= 150 || pulseVal <= 10) pulseDir = -pulseDir;
    }
    uint32_t color = (agentStatus == "thinking")
      ? ring.Color(0, 0, pulseVal)              // blue pulse
      : ring.Color(pulseVal / 2, 0, pulseVal);  // purple pulse
    ring.fill(color);
  } else if (agentStatus == "done") {
    ring.fill(ring.Color(0, 80, 0));   // solid green
  } else if (agentStatus == "error") {
    ring.fill(ring.Color(120, 0, 0));  // solid red
  } else {
    ring.fill(ring.Color(10, 10, 10)); // dim white = idle
  }
  ring.show();
#endif
}

// ---- WebSocket event handler ----
void onWebSocketEvent(uint8_t clientNum, WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      Serial.printf("[WS] Client %u disconnected\n", clientNum);
      break;

    case WStype_CONNECTED:
      Serial.printf("[WS] Client %u connected\n", clientNum);
      break;

    case WStype_TEXT: {
      // Parse incoming JSON
      JsonDocument doc;
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) {
        Serial.print("JSON parse error: ");
        Serial.println(err.c_str());
        return;
      }

      // Update state from JSON fields (all optional)
      if (doc["status"].is<const char*>())    agentStatus = doc["status"].as<String>();
      if (doc["tokens_in"].is<long>())        tokensIn    = doc["tokens_in"];
      if (doc["tokens_out"].is<long>())       tokensOut   = doc["tokens_out"];
      if (doc["cost"].is<float>())            costUSD     = doc["cost"];
      if (doc["model"].is<const char*>())     modelName   = doc["model"].as<String>();

      // Session management
      if (doc["session"].is<const char*>()) {
        String sess = doc["session"].as<String>();
        if (sess == "start") {
          sessionStartMs = millis();
          sessionActive = true;
          tokensIn = 0;
          tokensOut = 0;
          costUSD = 0.0;
        } else if (sess == "end") {
          sessionActive = false;
        }
      }

      Serial.printf("[DATA] status=%s in=%ld out=%ld cost=%.4f\n",
        agentStatus.c_str(), tokensIn, tokensOut, costUSD);
      break;
    }

    default:
      break;
  }
}

// ========== SETUP ==========
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== Claude Code Monitor ===");

  // Init OLED
  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("OLED init failed!");
    while (true) delay(1000);
  }
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println("Connecting WiFi...");
  display.display();

  // Init NeoPixel
#ifdef USE_NEOPIXEL
  ring.begin();
  ring.setBrightness(40);
  ring.fill(ring.Color(50, 50, 0)); // yellow = connecting
  ring.show();
#endif

  // Connect WiFi
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected!");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());

    display.clearDisplay();
    display.setCursor(0, 0);
    display.println("WiFi connected!");
    display.println();
    display.println("IP Address:");
    display.setTextSize(2);
    display.println(WiFi.localIP());
    display.setTextSize(1);
    display.println();
    display.println("Waiting for data...");
    display.display();
  } else {
    Serial.println("\nWiFi FAILED");
    display.clearDisplay();
    display.setCursor(0, 0);
    display.println("WiFi FAILED!");
    display.println("Check credentials");
    display.display();
  }

  // Start WebSocket server
  webSocket.begin();
  webSocket.onEvent(onWebSocketEvent);
  Serial.println("WebSocket server on :81");
}

// ========== LOOP ==========
void loop() {
  webSocket.loop();
  drawDisplay();
  updateLEDs();
  delay(50);  // ~20fps refresh
}
```

### Upload to ESP32

1. Plug ESP32 into your computer via USB
2. In Arduino IDE: **Tools → Board → ESP32 Dev Module**
3. **Tools → Port** → select the COM/serial port that appeared (e.g., COM3, /dev/cu.usbserial-xxxx)
4. **Edit the code**: change `YOUR_WIFI_NAME` and `YOUR_WIFI_PASSWORD` to your actual WiFi
5. Click the **Upload** button (→ arrow icon)
6. Wait for "Done uploading"
7. Open **Tools → Serial Monitor** (set baud to 115200)
8. You should see the IP address printed — **write it down**, you'll need it

---

## Part 6: Bridge Script (Python)

This script runs on your computer. It watches Claude Code CLI output and forwards data to the ESP32.

Save this as `claude_monitor_bridge.py` anywhere on your machine:

```python
#!/usr/bin/env python3
"""
Claude Code Hardware Monitor — Bridge Script

Watches Claude Code CLI output and sends stats to the ESP32
over WebSocket.

Usage:
    # Option A: Pipe Claude Code output through the bridge
    claude-code 2>&1 | python3 claude_monitor_bridge.py

    # Option B: Run standalone and point it at a log file
    python3 claude_monitor_bridge.py --log-file /tmp/claude.log

    # Option C: Run in demo mode to test your hardware
    python3 claude_monitor_bridge.py --demo
"""

import argparse
import json
import re
import sys
import time
import threading
import websocket

# ── Config ──────────────────────────────────────────────────
ESP32_IP = "YOUR_ESP32_IP"  # ← Change this to the IP shown on your OLED
ESP32_PORT = 81

# Rough pricing per token (update as needed)
# See: https://docs.anthropic.com/en/docs/about-claude/pricing
PRICE_PER_INPUT_TOKEN  = 15.0 / 1_000_000   # Opus
PRICE_PER_OUTPUT_TOKEN = 75.0 / 1_000_000   # Opus

# ── WebSocket connection ────────────────────────────────────
ws = None
ws_connected = False

def connect_ws():
    global ws, ws_connected
    url = f"ws://{ESP32_IP}:{ESP32_PORT}/"
    try:
        ws = websocket.WebSocket()
        ws.connect(url, timeout=5)
        ws_connected = True
        print(f"[bridge] Connected to ESP32 at {url}")
    except Exception as e:
        ws_connected = False
        print(f"[bridge] Cannot reach ESP32 at {url}: {e}")
        print("[bridge] Retrying in 5s...")
        time.sleep(5)
        connect_ws()

def send(data: dict):
    global ws, ws_connected
    try:
        if ws_connected:
            ws.send(json.dumps(data))
    except Exception:
        ws_connected = False
        print("[bridge] Lost connection, reconnecting...")
        connect_ws()

# ── Parse Claude Code output ──────────────────────────────
# These patterns match common Claude Code CLI output.
# They may need adjustment if the CLI format changes.

RE_TOKENS    = re.compile(r"tokens[:\s]+(\d[\d,]*)\s*/\s*(\d[\d,]*)", re.IGNORECASE)
RE_COST      = re.compile(r"\$\s*([\d.]+)", re.IGNORECASE)
RE_MODEL     = re.compile(r"(claude-[a-z0-9\-\.]+)", re.IGNORECASE)
RE_THINKING  = re.compile(r"(thinking|generating|processing)", re.IGNORECASE)
RE_TOOL      = re.compile(r"(tool use|running|executing|bash|read|write|search)", re.IGNORECASE)
RE_DONE      = re.compile(r"(done|complete|finished|response ready)", re.IGNORECASE)
RE_ERROR     = re.compile(r"(error|failed|rate.limit|exceeded)", re.IGNORECASE)

state = {
    "tokens_in": 0,
    "tokens_out": 0,
    "cost": 0.0,
    "model": "unknown",
    "status": "idle",
}

def parse_line(line: str):
    line = line.strip()
    if not line:
        return

    changed = False

    # Token counts
    m = RE_TOKENS.search(line)
    if m:
        state["tokens_in"]  = int(m.group(1).replace(",", ""))
        state["tokens_out"] = int(m.group(2).replace(",", ""))
        state["cost"] = (
            state["tokens_in"]  * PRICE_PER_INPUT_TOKEN +
            state["tokens_out"] * PRICE_PER_OUTPUT_TOKEN
        )
        changed = True

    # Explicit cost
    m = RE_COST.search(line)
    if m:
        state["cost"] = float(m.group(1))
        changed = True

    # Model name
    m = RE_MODEL.search(line)
    if m:
        state["model"] = m.group(1)
        changed = True

    # Status detection (order matters: error > tool > thinking > done)
    if RE_ERROR.search(line):
        state["status"] = "error"
        changed = True
    elif RE_TOOL.search(line):
        state["status"] = "tool_use"
        changed = True
    elif RE_THINKING.search(line):
        state["status"] = "thinking"
        changed = True
    elif RE_DONE.search(line):
        state["status"] = "done"
        changed = True

    if changed:
        send(state)

# ── Input modes ─────────────────────────────────────────────
def run_stdin():
    """Read from piped stdin (claude-code | bridge.py)"""
    send({"session": "start", "status": "idle"})
    print("[bridge] Reading from stdin. Pipe Claude Code output here.")
    for line in sys.stdin:
        parse_line(line)
    send({"session": "end", "status": "idle"})

def run_log_file(path: str):
    """Tail a log file"""
    send({"session": "start", "status": "idle"})
    print(f"[bridge] Tailing log file: {path}")
    import subprocess
    proc = subprocess.Popen(
        ["tail", "-f", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    try:
        for line in proc.stdout:
            parse_line(line)
    except KeyboardInterrupt:
        proc.kill()
    send({"session": "end", "status": "idle"})

def run_demo():
    """Send fake data to test the hardware display"""
    print("[bridge] Demo mode — sending test data to ESP32")

    send({"session": "start", "status": "idle", "model": "opus-4", "tokens_in": 0, "tokens_out": 0, "cost": 0.0})
    time.sleep(2)

    # Simulate thinking
    for i in range(10):
        tokens_in = (i + 1) * 500
        send({"status": "thinking", "tokens_in": tokens_in, "tokens_out": 0,
              "cost": tokens_in * PRICE_PER_INPUT_TOKEN, "model": "opus-4"})
        time.sleep(0.5)

    # Simulate tool use
    for i in range(5):
        t_in = 5000 + i * 200
        t_out = (i + 1) * 300
        send({"status": "tool_use", "tokens_in": t_in, "tokens_out": t_out,
              "cost": t_in * PRICE_PER_INPUT_TOKEN + t_out * PRICE_PER_OUTPUT_TOKEN,
              "model": "opus-4"})
        time.sleep(1)

    # Simulate done
    send({"status": "done", "tokens_in": 6000, "tokens_out": 1500,
          "cost": 6000 * PRICE_PER_INPUT_TOKEN + 1500 * PRICE_PER_OUTPUT_TOKEN,
          "model": "opus-4"})
    time.sleep(5)

    send({"session": "end", "status": "idle"})
    print("[bridge] Demo complete!")

# ── Main ────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Claude Code Hardware Monitor Bridge")
    parser.add_argument("--log-file", help="Path to Claude Code log file to tail")
    parser.add_argument("--demo", action="store_true", help="Send fake data to test hardware")
    parser.add_argument("--ip", help="ESP32 IP address (overrides script default)")
    parser.add_argument("--port", type=int, default=81, help="ESP32 WebSocket port")
    args = parser.parse_args()

    if args.ip:
        ESP32_IP = args.ip
    ESP32_PORT = args.port

    connect_ws()

    if args.demo:
        run_demo()
    elif args.log_file:
        run_log_file(args.log_file)
    else:
        run_stdin()
```

---

## Part 7: First Boot — Step by Step

### Test 1: Verify the screen works

1. Upload the firmware (Part 5)
2. The OLED should show "Connecting WiFi..." then your IP address
3. If you see the IP, the hardware is working

### Test 2: Demo mode

1. Edit `claude_monitor_bridge.py` — change `YOUR_ESP32_IP` to the IP shown on your OLED
2. Run:
   ```bash
   python3 claude_monitor_bridge.py --demo --ip 192.168.1.XXX
   ```
3. Watch the OLED cycle through: idle → thinking → tool use → done
4. If using the NeoPixel ring, the colors should pulse and change

### Test 3: Live with Claude Code

Pipe Claude Code's output through the bridge:

```bash
claude 2>&1 | tee /dev/tty | python3 claude_monitor_bridge.py --ip 192.168.1.XXX
```

This shows Claude Code output on your terminal normally (`tee /dev/tty`) while also feeding it to the bridge script.

---

## Part 8: Troubleshooting

| Problem | Fix |
|---|---|
| OLED is blank | Check wiring. Try I2C address `0x3D` instead of `0x3C` in the code. |
| "WiFi FAILED" on screen | Double-check SSID/password. ESP32 only supports 2.4GHz WiFi, not 5GHz. |
| Bridge can't connect | Make sure ESP32 and your computer are on the same WiFi network. Check the IP. |
| Upload fails in Arduino IDE | Hold the **BOOT** button on ESP32 when upload starts. Try a different USB cable (some are power-only). |
| Serial Monitor shows garbage | Set baud rate to **115200** in Serial Monitor. |
| Token counts not updating | The regex patterns may need tuning for your Claude Code version. Add `print(line)` in `parse_line()` to debug. |

---

## Part 9: Make It Your Own

Once the basic build works, here are upgrades to try:

- **3D print an enclosure** — Search Thingiverse for "ESP32 OLED case"
- **Bigger display** — Swap the 0.96" OLED for a 1.3" or a color TFT (ST7735). Change the display library accordingly.
- **Multiple sessions graph** — Store token history in ESP32's SPIFFS filesystem, draw a mini bar chart
- **Sound effects** — Add a small piezo buzzer that chirps when a task completes
- **Web dashboard** — The ESP32 can also serve a tiny HTML page at `http://<ip>/` showing the same stats in a browser

---

## Part 10: Share It

This project is viral bait. To maximize reach:

1. **Record a 30-second video**: Show you sending a Claude Code prompt, then cut to the physical display lighting up and counting tokens. Vertical format, subtitles.
2. **Post on**: Twitter/X, Reddit (r/arduino, r/ClaudeAI, r/homelab), YouTube Shorts, TikTok
3. **GitHub repo**: Include this README, a photo, the wiring diagram, both code files, and a BOM with purchase links
4. **Title formula**: *"I built a physical dashboard for Claude Code with a $20 ESP32"*

---

## Quick Reference: File List

| File | Where it runs | Purpose |
|---|---|---|
| Arduino sketch (.ino) | ESP32 | Runs the display + WebSocket server |
| `claude_monitor_bridge.py` | Your computer | Parses Claude Code output, sends to ESP32 |

## Quick Reference: Wiring

```
OLED VCC  → 3.3V
OLED GND  → GND
OLED SDA  → GPIO 21
OLED SCL  → GPIO 22
Neo VIN   → 5V     (optional)
Neo GND   → GND    (optional)
Neo DIN   → GPIO 13 via 470Ω (optional)
```

Happy building!
