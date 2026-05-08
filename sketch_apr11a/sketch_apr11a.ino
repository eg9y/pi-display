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
const char* WIFI_SSID     = "WhiteSky-Cornell";
const char* WIFI_PASSWORD = "wrf3rfq8";
// ==================================================


// ---- TCA9548A I2C multiplexer ----
#define TCA_ADDR 0x70

// Mux channels that have an OLED wired to them. Add more as you solder more tiles.
const uint8_t TILE_CHANNELS[] = {7, 6};
const uint8_t NUM_TILES = sizeof(TILE_CHANNELS) / sizeof(TILE_CHANNELS[0]);

// ---- Per-tile state ----
// Each OLED in TILE_CHANNELS[] is driven from its own TileState, addressed
// over the wire by 1-based `slot` (slot N → tiles[N-1] → TILE_CHANNELS[N-1]).
// Defined here (before any function signature) so the Arduino preprocessor's
// auto-generated forward declarations resolve the type.
struct TileState {
  String status;
  String prevStatus;
  String toolName;
  long   tokensIn;
  long   tokensOut;
  float  costUSD;
  String modelName;
  unsigned long sessionStartMs;
  unsigned long pausedElapsedMs;
  bool   timerPaused;
  bool   sessionActive;
  int    todoDone;
  int    todoTotal;
  bool   mascotAnimActive;
  unsigned long mascotAnimStartMs;
  bool   waitingAnimActive;
  unsigned long waitingAnimStartMs;
};

TileState tiles[NUM_TILES];

void tca_select(uint8_t ch) {
  Wire.beginTransmission(TCA_ADDR);
  Wire.write(1 << ch);
  Wire.endTransmission();
}

// OLED setup
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// Build the framebuffer once (in RAM), then push it to every tile.
// Used during boot screens and "WiFi failed" messages where every tile
// should mirror the same content. Per-agent rendering uses drawTile()
// instead, which selects a single channel.
void pushFrameToAllTiles() {
  for (uint8_t i = 0; i < NUM_TILES; i++) {
    tca_select(TILE_CHANNELS[i]);
    display.display();
  }
}

// Passive buzzer on GPIO 23 (same side as D21/D22 for easy breadboard access)
#define BUZZER_PIN 23

// ---- Keyboard buttons (one per tile, same index order as TILE_CHANNELS) ----
// Wire each switch between its GPIO and GND; INPUT_PULLUP handles the rest.
const uint8_t BUTTON_PINS[] = {4, 5};
static_assert(sizeof(BUTTON_PINS) / sizeof(BUTTON_PINS[0]) == NUM_TILES,
              "BUTTON_PINS must have one entry per tile");
const uint16_t DEBOUNCE_MS = 20;
uint8_t  btnRaw[NUM_TILES];
uint8_t  btnStable[NUM_TILES];
uint32_t btnLastChangeMs[NUM_TILES];

// WebSocket server on port 81
WebSocketsServer webSocket = WebSocketsServer(81);

// Optional NeoPixel
#ifdef USE_NEOPIXEL
#define NEO_PIN 13
#define NEO_COUNT 12
Adafruit_NeoPixel ring(NEO_COUNT, NEO_PIN, NEO_GRB + NEO_KHZ800);
#endif

// (TileState is defined near the top of the file — moved up so the
// Arduino preprocessor's auto-generated function prototypes can
// reference it without seeing "type does not name a type".)

void initTiles() {
  for (uint8_t i = 0; i < NUM_TILES; i++) {
    tiles[i].status = "idle";
    tiles[i].prevStatus = "idle";
    tiles[i].toolName = "";
    tiles[i].tokensIn = 0;
    tiles[i].tokensOut = 0;
    tiles[i].costUSD = 0.0;
    tiles[i].modelName = "—";
    tiles[i].sessionStartMs = 0;
    tiles[i].pausedElapsedMs = 0;
    tiles[i].timerPaused = false;
    tiles[i].sessionActive = false;
    tiles[i].todoDone = 0;
    tiles[i].todoTotal = 0;
    tiles[i].mascotAnimActive = false;
    tiles[i].mascotAnimStartMs = 0;
    tiles[i].waitingAnimActive = false;
    tiles[i].waitingAnimStartMs = 0;
  }
}

// ---- Animation ----
unsigned long lastPulse = 0;
int pulseVal = 0;
int pulseDir = 5;

const char SPINNER[] = {'|', '/', '-', '\\'};
unsigned long lastSpinnerMs = 0;
uint8_t spinnerIdx = 0;

int sweepPos = 0;
int sweepDir = 2;
unsigned long lastSweepMs = 0;

// Mascot "session done" animation — duration is shared, the per-tile
// `mascotAnimActive`/`mascotAnimStartMs` decide when it plays on each tile.
const unsigned long MASCOT_ANIM_DURATION_MS = 2200;

// ---- Buzzer: blocking note + jingle helpers ----
void playTone(int freq, int durMs) {
  tone(BUZZER_PIN, freq, durMs);
  delay(durMs);
  noTone(BUZZER_PIN);
}

void playJingle(const char* kind) {
  if (strcmp(kind, "done") == 0) {
    playTone(523, 90);   // C5
    playTone(659, 90);   // E5
    playTone(784, 160);  // G5
  } else if (strcmp(kind, "waiting") == 0) {
    playTone(880, 70);   // A5
    delay(60);
    playTone(880, 70);
  } else if (strcmp(kind, "boot") == 0) {
    playTone(784, 60);
    playTone(1047, 80);  // C6
  }
}

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

// ---- Draw one tile from its TileState ----
// Caller is responsible for selecting the mux channel before calling.
void drawTile(const TileState& t) {
  display.clearDisplay();

  // Row 1: Status bar
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);

  // Status icon
  if (t.status == "thinking") {
    display.print(SPINNER[spinnerIdx]);
    display.print(" THINKING");
  } else if (t.status == "tool_use") {
    display.print(SPINNER[spinnerIdx]);
    if (t.toolName.length() > 0) {
      String label = t.toolName;
      if (label.length() > 16) label = label.substring(0, 16);
      display.print(" ");
      display.print(label);
    } else {
      display.print(" TOOL USE");
    }
  } else if (t.status == "done") {
    display.print("\x04 DONE");      // diamond
  } else if (t.status == "error") {
    display.print("! ERROR");
  } else {
    display.print("- IDLE");
  }

  // Session timer on the right
  if (t.sessionActive && t.sessionStartMs > 0) {
    unsigned long elapsedMs = t.timerPaused
      ? t.pausedElapsedMs
      : (millis() - t.sessionStartMs);
    String elapsed = formatElapsed(elapsedMs);
    int16_t x1, y1;
    uint16_t w, h;
    display.getTextBounds(elapsed, 0, 0, &x1, &y1, &w, &h);
    display.setCursor(SCREEN_WIDTH - w, 0);
    display.print(elapsed);
  }

  // Divider line
  display.drawLine(0, 10, SCREEN_WIDTH, 10, SSD1306_WHITE);

  // Sweeping comet on the divider while session is running.
  // sweepPos is shared across tiles — the comet stays in sync, which
  // looks intentional rather than chaotic.
  bool sessionRunning = (t.status == "thinking" || t.status == "tool_use");
  if (sessionRunning) {
    display.drawFastHLine(sweepPos, 9,  5, SSD1306_WHITE);
    display.drawFastHLine(sweepPos, 11, 5, SSD1306_WHITE);
    display.drawPixel(sweepPos - 1, 10, SSD1306_WHITE);
    display.drawPixel(sweepPos + 5, 10, SSD1306_WHITE);
  }

  // Row 2: Token counts
  display.setCursor(0, 14);
  display.print("IN:");
  display.setCursor(20, 14);
  display.setTextSize(2);
  display.print(formatTokens(t.tokensIn));

  display.setTextSize(1);
  display.setCursor(0, 32);
  display.print("OUT:");
  display.setCursor(28, 32);
  display.setTextSize(2);
  display.print(formatTokens(t.tokensOut));

  // Row 3: Cost + Todo + Model
  display.setTextSize(1);
  display.setCursor(0, 52);
  display.print("$");
  display.print(String(t.costUSD, 4));

  int16_t x1m, y1m;
  uint16_t wm, hm;
  display.getTextBounds(t.modelName, 0, 0, &x1m, &y1m, &wm, &hm);
  display.setCursor(SCREEN_WIDTH - wm, 52);
  display.print(t.modelName);

  if (t.todoTotal > 0) {
    String todoStr = String(t.todoDone) + "/" + String(t.todoTotal);
    int16_t x1t, y1t;
    uint16_t wt, ht;
    display.getTextBounds(todoStr, 0, 0, &x1t, &y1t, &wt, &ht);
    int tx = SCREEN_WIDTH - wm - wt - 4;
    if (tx > 44) {
      display.setCursor(tx, 52);
      display.print(todoStr);
    }
  }

  display.display();
}

// ---- Clawd pixel mascot frames (matches reference image) ----
// 16x16, 1-bit — blocky creature: corner ears, rect body, square eyes, short legs
//
// Visual layout (rows 6-11 active, wide squat creature with 2-row brim & dot-feet):
//   ..############..   <- body top (12 wide, cols 2-13)
//   ..##.######.##..   <- single-px eye notches at cols 4 & 11
//   ################   <- brim top (full 16 wide, sticks out 2px each side)
//   ################   <- brim bottom
//   ..############..   <- body bottom
//   ....#.#..#.#....   <- 4 dot-feet in two pairs (cols 4,6,9,11)

const unsigned char PROGMEM CLAWD_FRAME_1[] = {
  0x00, 0x00,  // row 0
  0x00, 0x00,  // row 1
  0x00, 0x00,  // row 2
  0x00, 0x00,  // row 3
  0x00, 0x00,  // row 4
  0x00, 0x00,  // row 5
  0x3F, 0xFC,  // row 6  body top
  0x37, 0xEC,  // row 7  eye notches
  0xFF, 0xFF,  // row 8  brim top
  0xFF, 0xFF,  // row 9  brim bottom
  0x3F, 0xFC,  // row 10 body bottom
  0x0A, 0x50,  // row 11 feet inner (cols 4,6,9,11)
  0x00, 0x00,  // row 12
  0x00, 0x00,  // row 13
  0x00, 0x00,  // row 14
  0x00, 0x00   // row 15
};

// Frame 2: feet pairs shifted outward for sway/walk cycle
const unsigned char PROGMEM CLAWD_FRAME_2[] = {
  0x00, 0x00,  // row 0
  0x00, 0x00,  // row 1
  0x00, 0x00,  // row 2
  0x00, 0x00,  // row 3
  0x00, 0x00,  // row 4
  0x00, 0x00,  // row 5
  0x3F, 0xFC,  // row 6  body top
  0x37, 0xEC,  // row 7  eye notches
  0xFF, 0xFF,  // row 8  brim top
  0xFF, 0xFF,  // row 9  brim bottom
  0x3F, 0xFC,  // row 10 body bottom
  0x14, 0x28,  // row 11 feet outer (cols 3,5,10,12)
  0x00, 0x00,  // row 12
  0x00, 0x00,  // row 13
  0x00, 0x00,  // row 14
  0x00, 0x00   // row 15
};

// Blink frame: eye notches closed (body edge is solid)
const unsigned char PROGMEM CLAWD_FRAME_BLINK[] = {
  0x00, 0x00,  // row 0
  0x00, 0x00,  // row 1
  0x00, 0x00,  // row 2
  0x00, 0x00,  // row 3
  0x00, 0x00,  // row 4
  0x00, 0x00,  // row 5
  0x3F, 0xFC,  // row 6  body top
  0x3F, 0xFC,  // row 7  eyes closed (no notches)
  0xFF, 0xFF,  // row 8  brim top
  0xFF, 0xFF,  // row 9  brim bottom
  0x3F, 0xFC,  // row 10 body bottom
  0x0A, 0x50,  // row 11 feet inner
  0x00, 0x00,  // row 12
  0x00, 0x00,  // row 13
  0x00, 0x00,  // row 14
  0x00, 0x00   // row 15
};

// Hop frame: feet tucked into body (mid-jump pose)
const unsigned char PROGMEM CLAWD_FRAME_HOP[] = {
  0x00, 0x00,  // row 0
  0x00, 0x00,  // row 1
  0x00, 0x00,  // row 2
  0x00, 0x00,  // row 3
  0x00, 0x00,  // row 4
  0x00, 0x00,  // row 5
  0x3F, 0xFC,  // row 6  body top
  0x37, 0xEC,  // row 7  eye notches
  0xFF, 0xFF,  // row 8  brim top
  0xFF, 0xFF,  // row 9  brim bottom
  0x3F, 0xFC,  // row 10 body bottom
  0x00, 0x00,  // row 11 feet tucked (no feet visible)
  0x00, 0x00,  // row 12
  0x00, 0x00,  // row 13
  0x00, 0x00,  // row 14
  0x00, 0x00   // row 15
};

// ---- Full-screen Clawd mascot animation on session done ----
void drawMascotAnimation(unsigned long elapsed) {
  display.clearDisplay();

  // 4-phase bouncy walk cycle: stand -> hop-left -> land-wide -> hop-right
  // 150ms per phase = 600ms full cycle
  int phase = (elapsed / 150) % 4;
  static const int bobTable[4] = {0, -2, 0, -2};
  static const int dxTable[4]  = {0, -1, 0, +1};
  const unsigned char* walkFrames[4] = {
    CLAWD_FRAME_1,    // stand
    CLAWD_FRAME_HOP,  // mid-hop (lean left)
    CLAWD_FRAME_2,    // land with feet wide
    CLAWD_FRAME_HOP   // mid-hop (lean right)
  };

  bool blink = ((elapsed / 120) % 10 == 9);
  const unsigned char* frame = blink ? CLAWD_FRAME_BLINK : walkFrames[phase];

  // scale 16x16 sprite up to 48x48 for visibility on 128x64 OLED
  int scale = 3;
  int spriteW = 16 * scale;
  int spriteH = 16 * scale;

  int x = (SCREEN_WIDTH - spriteW) / 2 + dxTable[phase];
  int y = bobTable[phase];

  // draw scaled bitmap
  for (int row = 0; row < 16; row++) {
    uint8_t leftByte  = pgm_read_byte(&frame[row * 2]);
    uint8_t rightByte = pgm_read_byte(&frame[row * 2 + 1]);
    uint16_t bits = ((uint16_t)leftByte << 8) | rightByte;

    for (int col = 0; col < 16; col++) {
      if (bits & (1 << (15 - col))) {
        display.fillRect(
          x + col * scale,
          y + row * scale,
          scale,
          scale,
          SSD1306_WHITE
        );
      }
    }
  }

  // flashing banner at bottom
  if ((elapsed / 250) % 2 == 0) {
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    const char* msg = "CLAWD DONE!";
    int16_t x1, y1;
    uint16_t w, h;
    display.getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
    display.setCursor((SCREEN_WIDTH - w) / 2, 56);
    display.print(msg);
  }

  display.display();
}

// ---- "Waiting for user" animation (persistent, mascot + flashing banner) ----
void drawWaitingAnimation(unsigned long elapsed) {
  display.clearDisplay();

  // Same 4-phase walk as DONE, but slower & gentler so it doesn't get annoying
  // 250ms per phase = 1000ms full cycle, 1px hop
  int phase = (elapsed / 250) % 4;
  static const int bobTable[4] = {0, -1, 0, -1};
  static const int dxTable[4]  = {0, -1, 0, +1};
  const unsigned char* walkFrames[4] = {
    CLAWD_FRAME_1,
    CLAWD_FRAME_HOP,
    CLAWD_FRAME_2,
    CLAWD_FRAME_HOP
  };

  bool blink = ((elapsed / 120) % 12 == 10);
  const unsigned char* frame = blink ? CLAWD_FRAME_BLINK : walkFrames[phase];

  int scale = 3;
  int spriteW = 16 * scale;
  int x = (SCREEN_WIDTH - spriteW) / 2 + dxTable[phase];
  int y = bobTable[phase];

  for (int row = 0; row < 16; row++) {
    uint8_t leftByte  = pgm_read_byte(&frame[row * 2]);
    uint8_t rightByte = pgm_read_byte(&frame[row * 2 + 1]);
    uint16_t bits = ((uint16_t)leftByte << 8) | rightByte;
    for (int col = 0; col < 16; col++) {
      if (bits & (1 << (15 - col))) {
        display.fillRect(x + col * scale, y + row * scale, scale, scale, SSD1306_WHITE);
      }
    }
  }

  // Flashing "YOUR TURN!" banner at bottom
  if ((elapsed / 400) % 2 == 0) {
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    const char* msg = "YOUR TURN!";
    int16_t x1, y1;
    uint16_t w, h;
    display.getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
    display.setCursor((SCREEN_WIDTH - w) / 2, 56);
    display.print(msg);
  }

  display.display();
}

// ---- Update NeoPixel ring based on status ----
// One ring, many tiles → pick the "loudest" status across all tiles so
// any active agent lights up the room. Priority: error > tool_use > thinking
// > done > idle. Later we can map ring segments per slot.
void updateLEDs() {
#ifdef USE_NEOPIXEL
  unsigned long now = millis();

  String agg = "idle";
  for (uint8_t i = 0; i < NUM_TILES; i++) {
    const String& s = tiles[i].status;
    if (s == "error")                               { agg = "error"; break; }
    else if (s == "tool_use")                       agg = "tool_use";
    else if (s == "thinking" && agg != "tool_use")  agg = "thinking";
    else if (s == "done" && agg == "idle")          agg = "done";
  }

  if (agg == "thinking" || agg == "tool_use") {
    if (now - lastPulse > 30) {
      lastPulse = now;
      pulseVal += pulseDir;
      if (pulseVal >= 150 || pulseVal <= 10) pulseDir = -pulseDir;
    }
    uint32_t color = (agg == "thinking")
      ? ring.Color(0, 0, pulseVal)
      : ring.Color(pulseVal / 2, 0, pulseVal);
    ring.fill(color);
  } else if (agg == "done") {
    ring.fill(ring.Color(0, 80, 0));
  } else if (agg == "error") {
    ring.fill(ring.Color(120, 0, 0));
  } else {
    ring.fill(ring.Color(10, 10, 10));
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
      JsonDocument doc;
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) {
        Serial.print("JSON parse error: ");
        Serial.println(err.c_str());
        return;
      }

      // Slot routing. Slot 0 (or missing) means "broadcast to all tiles" —
      // used for boot/connect and any payload before slot binding lands.
      int slot = 0;
      if (doc["slot"].is<int>()) slot = doc["slot"].as<int>();

      // One-shot events: per-tile animations + a single buzzer ding.
      if (doc["event"].is<const char*>()) {
        String ev = doc["event"].as<String>();
        auto applyEvent = [&](TileState& t) {
          if (ev == "session_done") {
            t.mascotAnimActive = true;
            t.mascotAnimStartMs = millis();
          } else if (ev == "waiting_for_user") {
            t.waitingAnimActive = true;
            t.waitingAnimStartMs = millis();
          }
        };
        if (slot >= 1 && slot <= (int)NUM_TILES) {
          applyEvent(tiles[slot - 1]);
        } else {
          for (uint8_t i = 0; i < NUM_TILES; i++) applyEvent(tiles[i]);
        }
        // Jingle once regardless of slot — the buzzer is shared hardware.
        if (ev == "session_done") playJingle("done");
        else if (ev == "waiting_for_user") playJingle("waiting");
        return;
      }

      auto applyState = [&](TileState& t) {
        if (doc["status"].is<const char*>()) {
          t.status = doc["status"].as<String>();
          if (t.waitingAnimActive && (t.status == "thinking" || t.status == "tool_use")) {
            t.waitingAnimActive = false;
          }
        }
        if (doc["tokens_in"].is<long>())        t.tokensIn  = doc["tokens_in"];
        if (doc["tokens_out"].is<long>())       t.tokensOut = doc["tokens_out"];
        if (doc["cost"].is<float>())            t.costUSD   = doc["cost"];
        if (doc["model"].is<const char*>())     t.modelName = doc["model"].as<String>();
        if (doc["todo_done"].is<int>())         t.todoDone  = doc["todo_done"];
        if (doc["todo_total"].is<int>())        t.todoTotal = doc["todo_total"];
        if (doc["tool_name"].is<const char*>()) t.toolName  = doc["tool_name"].as<String>();
        else                                    t.toolName  = "";

        if (t.status != t.prevStatus) {
          if (t.status == "done") {
            if (!t.timerPaused && t.sessionActive) {
              t.pausedElapsedMs = millis() - t.sessionStartMs;
              t.timerPaused = true;
            }
          } else if (t.status == "thinking" || t.status == "tool_use") {
            if (t.prevStatus == "done" || t.prevStatus == "idle" || !t.sessionActive) {
              t.sessionStartMs = millis();
              t.pausedElapsedMs = 0;
              t.timerPaused = false;
              t.sessionActive = true;
            }
          }
          t.prevStatus = t.status;
        }

        if (doc["session"].is<const char*>()) {
          String sess = doc["session"].as<String>();
          if (sess == "start") {
            t.sessionStartMs = millis();
            t.sessionActive = true;
            t.timerPaused = false;
            t.pausedElapsedMs = 0;
            t.tokensIn = 0;
            t.tokensOut = 0;
            t.costUSD = 0.0;
          } else if (sess == "end") {
            t.sessionActive = false;
          }
        }
      };

      if (slot >= 1 && slot <= (int)NUM_TILES) {
        applyState(tiles[slot - 1]);
        Serial.printf("[DATA] slot=%d status=%s in=%ld out=%ld cost=%.4f\n",
          slot, tiles[slot - 1].status.c_str(),
          tiles[slot - 1].tokensIn, tiles[slot - 1].tokensOut, tiles[slot - 1].costUSD);
      } else {
        for (uint8_t i = 0; i < NUM_TILES; i++) applyState(tiles[i]);
        Serial.printf("[DATA] broadcast status=%s\n", tiles[0].status.c_str());
      }
      break;
    }

    default:
      break;
  }
}

// ---- Buttons ----
void setupButtons() {
  for (uint8_t i = 0; i < NUM_TILES; i++) {
    pinMode(BUTTON_PINS[i], INPUT_PULLUP);
    btnRaw[i] = HIGH;
    btnStable[i] = HIGH;
    btnLastChangeMs[i] = 0;
  }
}

void pollButtons() {
  uint32_t now = millis();
  for (uint8_t i = 0; i < NUM_TILES; i++) {
    uint8_t raw = digitalRead(BUTTON_PINS[i]);
    if (raw != btnRaw[i]) {
      btnRaw[i] = raw;
      btnLastChangeMs[i] = now;
    }
    if ((now - btnLastChangeMs[i]) >= DEBOUNCE_MS && raw != btnStable[i]) {
      btnStable[i] = raw;
      if (raw == LOW) {
        int slot = i + 1;   // 1-indexed so it matches tmux agent-N convention
        char buf[48];
        snprintf(buf, sizeof(buf), "{\"event\":\"focus\",\"slot\":%d}", slot);
        webSocket.broadcastTXT(buf);
        Serial.printf("[BTN] focus slot=%d\n", slot);
      }
    }
  }
}

// ========== SETUP ==========
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== Claude Code Monitor ===");

  initTiles();
  setupButtons();

  // Bring up I2C, then init every OLED tile by selecting its mux channel first.
  // display.begin() allocates the framebuffer once and re-runs the SSD1306 init
  // sequence on subsequent calls, so looping here powers up each tile in turn.
  Wire.begin();
  for (uint8_t i = 0; i < NUM_TILES; i++) {
    tca_select(TILE_CHANNELS[i]);
    if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
      Serial.printf("OLED init failed on ch%d!\n", TILE_CHANNELS[i]);
    }
    display.clearDisplay();
    display.display();
  }

  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println("Connecting WiFi...");
  pushFrameToAllTiles();

  // Init NeoPixel
#ifdef USE_NEOPIXEL
  ring.begin();
  ring.setBrightness(40);
  ring.fill(ring.Color(50, 50, 0)); // yellow = connecting
  ring.show();
#endif

  // ---- WiFi diagnostics ----
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true, true);
  delay(100);

  Serial.print("ESP32 MAC: ");
  Serial.println(WiFi.macAddress());

  Serial.println("Scanning for networks...");
  int n = WiFi.scanNetworks();
  Serial.printf("Found %d networks:\n", n);
  bool sawTarget = false;
  for (int i = 0; i < n; i++) {
    bool isTarget = WiFi.SSID(i) == String(WIFI_SSID);
    if (isTarget) sawTarget = true;
    // Auth modes: 0=open, 1=WEP, 2=WPA-PSK, 3=WPA2-PSK, 4=WPA/WPA2-PSK,
    //             5=WPA2-Enterprise, 6=WPA3-PSK, 7=WPA2/WPA3-PSK
    Serial.printf("  %s%-32s ch=%2d rssi=%3d auth=%d\n",
                  isTarget ? "* " : "  ",
                  WiFi.SSID(i).c_str(),
                  WiFi.channel(i),
                  WiFi.RSSI(i),
                  (int)WiFi.encryptionType(i));
  }
  if (!sawTarget) {
    Serial.printf("WARNING: '%s' NOT visible to ESP32 (likely 5GHz-only or out of range).\n", WIFI_SSID);
  }

  // Log disconnect reasons so we know whether it's auth, assoc, or timeout.
  WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
    Serial.printf("WiFi disconnect, reason=%u\n", info.wifi_sta_disconnected.reason);
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);

  Serial.printf("Connecting to '%s'...\n", WIFI_SSID);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WiFi connected!");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());
    Serial.print("RSSI: ");
    Serial.println(WiFi.RSSI());

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
    pushFrameToAllTiles();
  } else {
    // WL_* codes: 0=IDLE 1=NO_SSID_AVAIL 2=SCAN_COMPLETED 3=CONNECTED
    //             4=CONNECT_FAILED 5=CONNECTION_LOST 6=DISCONNECTED
    wl_status_t s = WiFi.status();
    Serial.printf("WiFi FAILED, final status=%d\n", (int)s);
    display.clearDisplay();
    display.setCursor(0, 0);
    display.println("WiFi FAILED!");
    display.printf("status=%d\n", (int)s);
    display.println(sawTarget ? "SSID visible" : "SSID NOT seen");
    pushFrameToAllTiles();
  }

  // Start WebSocket server
  webSocket.begin();
  webSocket.onEvent(onWebSocketEvent);
  Serial.println("WebSocket server on :81");

  // Proof-of-life chirp so you know the buzzer is wired right.
  playJingle("boot");
}

// ========== LOOP ==========
void loop() {
  webSocket.loop();
  pollButtons();

  uint32_t now = millis();
  if (now - lastSpinnerMs > 120) {
    lastSpinnerMs = now;
    spinnerIdx = (spinnerIdx + 1) % 4;
  }
  // Advance the divider comet once per loop so it stays in sync across tiles.
  if (now - lastSweepMs > 30) {
    lastSweepMs = now;
    sweepPos += sweepDir;
    if (sweepPos <= 0 || sweepPos >= SCREEN_WIDTH - 6) sweepDir = -sweepDir;
  }

  // Render each tile from its own state, on its own mux channel.
  for (uint8_t i = 0; i < NUM_TILES; i++) {
    tca_select(TILE_CHANNELS[i]);
    TileState& t = tiles[i];

    if (t.waitingAnimActive) {
      drawWaitingAnimation(now - t.waitingAnimStartMs);
    } else if (t.mascotAnimActive) {
      unsigned long elapsed = now - t.mascotAnimStartMs;
      if (elapsed >= MASCOT_ANIM_DURATION_MS) {
        t.mascotAnimActive = false;
        drawTile(t);
      } else {
        drawMascotAnimation(elapsed);
      }
    } else {
      drawTile(t);
    }
  }

  updateLEDs();
  delay(33);  // ~30fps across all tiles
}
