import type {
  ExtensionAPI,
  ExtensionContext,
  AgentStartEvent,
  TurnEndEvent,
  ToolExecutionStartEvent,
  ToolExecutionEndEvent,
  MessageEndEvent,
  SessionStartEvent,
  SessionShutdownEvent,
  ToolCallEvent,
} from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";

// ═════════════════════════════════════════════════════════════════════════════
// hardware-bridge — Pi↔ESP32 hardware monitor extension
// ═════════════════════════════════════════════════════════════════════════════
//
// Connects pi directly to your ESP32 OLED console. No file tailing, no
// polling, no Python bridge. Instant event-driven sync.
//
// Features:
//  • Real-time token/cost/model/status push on every turn
//  • Bi-directional WebSocket: hardware buttons → focus tmux sessions
//  • Custom tool: LLM can send messages/animations to OLED tiles
//  • TUI footer widget showing connection state + last update
//  • Auto-reconnect with exponential backoff
//
// Usage:
//  1. Set ESP32 IP via settings.json or --hardware-ip flag
//  2. Start pi in your project — hardware bridge auto-connects
//  3. Press a key on the console → tmux focuses matching session
//  4. LLM can call hardware_display() to show custom messages
//
// Storage (in pi session):
//  customType: "hw-bridge"
//    { config: { esp32Ip, port } }
//
// ═════════════════════════════════════════════════════════════════════════════

interface BridgeConfig {
  esp32Ip: string;
  port: number;
  numTiles: number;
  terminalApp: string;
  tmuxPrefix: string;
}

interface TileState {
  status: string;
  tokens_in: number;
  tokens_out: number;
  cost: number;
  model: string;
  tool_name?: string;
}

const DEFAULT_CONFIG: BridgeConfig = {
  esp32Ip: "192.168.1.42",
  port: 81,
  numTiles: 2,
  terminalApp: "Ghostty",
  tmuxPrefix: "agent",
};

const OVERRIDES: Partial<BridgeConfig> = {};

function getConfig(): BridgeConfig {
  return { ...DEFAULT_CONFIG, ...OVERRIDES };
}

// Keep track of state across the session
class HardwareBridge {
  private ws: WebSocket | null = null;
  private connected = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private backoff = 1.0;
  private state: Map<number, TileState> = new Map();
  private currentSlot = 1;
  private lastPushMs = 0;
  private pendingBatch: Array<Record<string, any>> = [];
  private batchTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private pi: ExtensionAPI) {}

  connect(ip?: string, port?: number) {
    const cfg = getConfig();
    const target = ip ?? cfg.esp32Ip;
    const p = port ?? cfg.port;
    const url = `ws://${target}:${p}/`;

    this.disconnect();

    console.log(`[hw-bridge] connecting to ${url}`);
    try {
      this.ws = new WebSocket(url);

      this.ws.onopen = () => {
        this.connected = true;
        this.backoff = 1.0;
        console.log(`[hw-bridge] connected to ${url}`);
        this._notify("Hardware monitor connected", "success");
        // Send a heartbeat/identify ping
        this._send(JSON.stringify({ event: "pi_connected", version: "1.0" }));
        // Replay cached state for each active tile
        for (const [slot, tile] of this.state) {
          this._send(JSON.stringify({ ...tile, slot }));
        }
      };

      this.ws.onmessage = (ev) => {
        this._handleInbound(ev.data as string);
      };

      this.ws.onclose = () => {
        this.connected = false;
        this.ws = null;
        this._scheduleReconnect(target, p);
      };

      this.ws.onerror = (err) => {
        console.error("[hw-bridge] websocket error:", err);
        this.ws?.close();
      };
    } catch (e) {
      console.error("[hw-bridge] failed to create websocket:", e);
      this._scheduleReconnect(target, p);
    }
  }

  disconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.batchTimer) {
      clearTimeout(this.batchTimer);
      this.batchTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch {}
      this.ws = null;
    }
    this.connected = false;
  }

  private _scheduleReconnect(ip: string, port: number) {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect(ip, port);
    }, this.backoff * 1000);
    this.backoff = Math.min(this.backoff * 1.5, 30);
    console.log(`[hw-bridge] reconnecting in ${this.backoff}s`);
  }

  private _send(data: string): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try {
      this.ws.send(data);
      this.lastPushMs = Date.now();
      return true;
    } catch (e) {
      console.error("[hw-bridge] send failed:", e);
      this.ws.close();
      return false;
    }
  }

  private _sendBatch() {
    if (this.pendingBatch.length === 0) return;
    const merged = this.pendingBatch.reduce((acc, msg) => ({ ...acc, ...msg }), {});
    this.pendingBatch = [];
    // Remove duplicate slot keys if present — last one wins
    delete (merged as any).slot;
    this._send(JSON.stringify(merged));
  }

  private _queueBatch(payload: Record<string, any>) {
    this.pendingBatch.push(payload);
    if (!this.batchTimer) {
      this.batchTimer = setTimeout(() => {
        this.batchTimer = null;
        this._sendBatch();
      }, 16); // 60fps - coalesce rapid updates
    }
  }

  // ── Push tile state to hardware ──
  pushTile(slot: number, tile: Partial<TileState>) {
    const current = this.state.get(slot) ?? {
      status: "idle",
      tokens_in: 0,
      tokens_out: 0,
      cost: 0,
      model: "—",
    };
    const merged = { ...current, ...tile };
    this.state.set(slot, merged);

    const payload = {
      ...merged,
      slot,
    };
    this._queueBatch(payload);
  }

  // ── Push one-shot event (buzzer, animation, focus) ──
  pushEvent(slot: number, event: string) {
    const payload = { event, slot };
    this._send(JSON.stringify(payload));
  }

  // ── Handle inbound from ESP32 (button presses, etc.) ──
  private async _handleInbound(raw: string) {
    try {
      const msg = JSON.parse(raw);
      console.log("[hw-bridge] inbound:", msg);

      if (msg.event === "focus") {
        const slot = typeof msg.slot === "number" ? msg.slot : 1;
        await this._handleFocus(slot);
      } else if (msg.event === "button_press") {
        const slot = typeof msg.slot === "number" ? msg.slot : 1;
        await this._handleFocus(slot);
      }
    } catch (e) {
      // Not JSON — ignore
    }
  }

  private async _handleFocus(slot: number) {
    const cfg = getConfig();
    const target = `${cfg.tmuxPrefix}-${slot}`;

    try {
      // Try tmux switch
      await this._exec("tmux", ["switch-client", "-t", target]);
      // Raise terminal
      await this._exec("osascript", [
        "-e",
        `tell application "${cfg.terminalApp}" to activate`,
      ]);
      console.log(`[hw-bridge] focused tmux session: ${target}`);
      this._notify(`Focused ${target}`, "info");
    } catch (e) {
      console.error(`[hw-bridge] focus failed for ${target}:`, e);
    }
  }

  // ── Notifications ──
  private _notify(text: string, style: "info" | "success" | "error" = "info") {
    // Try to use pi's notification system when available in context
    // This is a no-op if called outside of an event handler context
    // We'll queue it for the next UI context
  }

  private _exec(cmd: string, args: string[]): Promise<void> {
    return new Promise((resolve, reject) => {
      const { spawn } = require("node:child_process");
      const proc = spawn(cmd, args, { stdio: "pipe" });
      let stderr = "";
      proc.stderr?.on("data", (d: any) => {
        stderr += String(d);
      });
      proc.on("close", (code: number | null) => {
        if (code === 0 || code === null) resolve();
        else reject(new Error(`Exit ${code}: ${stderr}`));
      });
      proc.on("error", reject);
    });
  }

  // ── Status for TUI ──
  getStatus(): string {
    if (this.connected) {
      const elapsed = Date.now() - this.lastPushMs;
      return `hw ${this.state.size}tile${this.state.size !== 1 ? "s" : ""}`;
    }
    return "hw offline";
  }

  getConnected(): boolean {
    return this.connected;
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// Extension factory
// ═════════════════════════════════════════════════════════════════════════════

export default function (pi: ExtensionAPI) {
  let bridge: HardwareBridge | null = null;

  // ── Load persisted config from session entries ──
  function loadConfig(ctx: ExtensionContext) {
    for (const entry of ctx.sessionManager.getEntries()) {
      if (entry.type === "custom" && entry.customType === "hw-bridge" && entry.data) {
        const data = entry.data as Record<string, any>;
        if (data.config) {
          Object.assign(OVERRIDES, data.config);
          console.log("[hw-bridge] loaded config from session:", data.config);
        }
      }
    }
  }

  // ── Persist config ──
  function saveConfig(cfg: Partial<BridgeConfig>) {
    Object.assign(OVERRIDES, cfg);
    pi.appendEntry("hw-bridge", { config: { ...getConfig() } });
  }

  // ── Register custom tool: hardware_display ──
  // The LLM can push custom messages to OLED tiles
  pi.registerTool({
    name: "hardware_display",
    label: "HW Display",
    description:
      "Send a custom message or animation to the hardware OLED display. " +
      "Useful to alert the user visually (e.g., 'build done', 'review needed').",
    promptSnippet: "Show a message on the hardware OLED console",
    promptGuidelines: [
      "Use hardware_display when there's a user-facing result that deserves attention on the physical desk display.",
      "Prefer slot 0 to broadcast to all tiles, or a specific slot for targeted notification.",
    ],
    parameters: Type.Object({
      slot: Type.Number({
        default: 0,
        description: "Display tile slot (0=broadcast, 1-8=specific tile)",
      }),
      event: Type.Optional(
        Type.String({
          description: "Animation preset: 'session_done', 'waiting_for_user', 'focus', or custom string.",
        })
      ),
      message: Type.Optional(
        Type.String({ description: "Short text to display (max 32 chars for one line)" })
      ),
      duration_ms: Type.Optional(
        Type.Number({ default: 3000, description: "How long to show the message" })
      ),
    }),
    async execute(_toolCallId, params, _signal, onUpdate) {
      if (!bridge) {
        return {
          content: [{ type: "text", text: "Hardware bridge not connected." }],
          details: { error: "no_bridge" },
        };
      }

      onUpdate?.({
        content: [{ type: "text", text: `Pushing to hardware slot ${params.slot}...` }],
      });

      const promises: Promise<void>[] = [];
      const cfg = getConfig();
      const slots =
        params.slot === 0 ? Array.from({ length: cfg.numTiles }, (_, i) => i + 1) : [params.slot];

      for (const slot of slots) {
        if (params.event) {
          bridge.pushEvent(slot, params.event);
        }
        if (params.message) {
          bridge.pushTile(slot, { status: params.message });
          // Reset after duration
          const dur = params.duration_ms ?? 3000;
          promises.push(
            new Promise((resolve) => {
              setTimeout(() => {
                // Revert to previous state if still showing the message
                const current = bridge!.state.get(slot);
                if (current?.status === params.message) {
                  bridge!.pushTile(slot, { status: "idle" });
                }
                resolve();
              }, dur);
            })
          );
        }
      }

      await Promise.all(promises);

      return {
        content: [{ type: "text", text: `Sent to hardware slot(s): ${slots.join(", ")}` }],
        details: { slots, event: params.event, message: params.message },
      };
    },
  });

  // ── WebSocket listener: forward events to hardware ──
  function attachBridgeEvents() {
    if (!bridge) return;

    // On every turn end → push stats
    pi.on("turn_end", async (event: TurnEndEvent) => {
      const msg = event.message;
      if (!msg || msg.role !== "assistant") return;

      const usage = msg.usage!;
      const cost = usage.cost?.total ?? 0;
      const model = msg.model ?? "—";
      const stopReason = msg.stopReason ?? "";
      let status = "thinking";
      let toolName = "";

      if (stopReason === "toolUse" || stopReason === "tool_use") {
        status = "tool_use";
        // Extract tool name from content
        const toolCalls = (msg.content as any[])?.filter(
          (c) => c.type === "toolCall"
        ) as Array<{ name: string }> | undefined;
        toolName = toolCalls?.[toolCalls.length - 1]?.name ?? "";
      } else if (["stop", "length", "aborted"].includes(stopReason)) {
        status = "done";
        // Send done event for buzzer
        bridge!.pushEvent(1, "session_done");
      } else if (stopReason === "error") {
        status = "error";
      }

      bridge!.pushTile(1, {
        status,
        tokens_in: usage.input + usage.cacheRead + usage.cacheWrite,
        tokens_out: usage.output,
        cost,
        model,
        tool_name: toolName,
      });

      // If done, also fire done event (buzzer)
      if (status === "done") {
        bridge!.pushEvent(1, "session_done");
      }
    });

    // When tool execution starts → update status early
    pi.on("tool_execution_start", async (event: ToolExecutionStartEvent) => {
      bridge!.pushTile(1, {
        status: "tool_use",
        tool_name: event.toolName,
      });
    });

    // When tool execution ends → update back to thinking
    pi.on("tool_execution_end", async (event: ToolExecutionEndEvent) => {
      bridge!.pushTile(1, {
        status: "thinking",
        tool_name: "",
      });
      // If was the last tool call, done will come in turn_end
    });

    // On user message → reset to thinking
    pi.on("message_start", async (event) => {
      if (event.message.role === "user") {
        bridge!.pushTile(1, {
          status: "thinking",
          tokens_in: 0,
          tokens_out: 0,
          cost: 0,
          tool_name: "",
        });
      }
    });

    // On model change → update display
    pi.on("model_select", async (event) => {
      const model = `${event.model.provider}/${event.model.id}`;
      bridge!.pushTile(1, { model });
    });
  }

  // ── Startup ──
  pi.on("session_start", async (event: SessionStartEvent, ctx: ExtensionContext) => {
    loadConfig(ctx);
    bridge = new HardwareBridge(pi);
    bridge.connect();

    // Attach event listeners after a tick to capture everything
    setTimeout(() => attachBridgeEvents(), 0);

    // Set up TUI footer
    if (ctx.hasUI) {
      ctx.ui.setStatus("hw-bridge", "Bridge starting...");
    }
  });

  // ── Status ticker ──
  pi.on("turn_start", async (_event, ctx) => {
    if (ctx.hasUI && bridge) {
      ctx.ui.setStatus("hw-bridge", bridge.getStatus());
    }
  });

  // ── Shutdown ──
  pi.on("session_shutdown", async (_event) => {
    bridge?.disconnect();
    bridge = null;
  });

  // ══════════════════════════════════════════════════════════════════════════
  // Commands
  // ══════════════════════════════════════════════════════════════════════════

  pi.registerCommand("hw-connect", {
    description: "(Re)connect hardware bridge to ESP32",
    handler: async (args, ctx) => {
      const ip = args.trim() || getConfig().esp32Ip;
      bridge?.connect(ip);
      ctx.ui.notify(`Connecting to ${ip}...`, "info");
    },
  });

  pi.registerCommand("hw-disconnect", {
    description: "Disconnect hardware bridge",
    handler: async (_args, ctx) => {
      bridge?.disconnect();
      ctx.ui.notify("Hardware bridge disconnected", "info");
    },
  });

  pi.registerCommand("hw-status", {
    description: "Show hardware bridge status",
    handler: async (_args, ctx) => {
      if (!bridge) {
        ctx.ui.notify("Hardware bridge not initialized", "error");
        return;
      }
      const connected = bridge.getConnected();
      const tiles = Array.from(bridge["state"].entries())
        .map(([s, t]) => `  slot ${s}: ${t.status} ${t.model} $${(t.cost ?? 0).toFixed(4)}`)
        .join("\n");
      ctx.ui.notify(
        `${connected ? "connected" : "offline"}\n${tiles || "  no tiles"}`,
        connected ? "success" : "error"
      );
    },
  });

  pi.registerCommand("hw-config", {
    description: "Set hardware bridge config (e.g. /hw-config ip=192.168.1.50 tiles=4)",
    handler: async (args, ctx) => {
      const [ip, tilesStr, termStr] = args.split(/\s+/).filter(Boolean);
      const update: Partial<BridgeConfig> = {};
      if (ip) {
        const match = ip.match(/(?:ip=)?([\d.]+)/);
        if (match) update.esp32Ip = match[1];
      }
      if (tilesStr) {
        const match = tilesStr.match(/(?:tiles=)?(\d+)/);
        if (match) update.numTiles = parseInt(match[1]);
      }
      if (termStr) {
        const match = termStr.match(/(?:terminal=)?(.+)/);
        if (match) update.terminalApp = match[1];
      }
      saveConfig(update);
      ctx.ui.notify(`Config saved: ${JSON.stringify(update)}`, "success");
      bridge?.connect(update.esp32Ip);
    },
  });

  // ══════════════════════════════════════════════════════════════════════════
  // CLI flag: --hardware-ip
  // ══════════════════════════════════════════════════════════════════════════
  pi.registerFlag("hardware-ip", {
    description: "ESP32 IP address for hardware monitor",
    type: "string",
  });

  // Check if flag was passed
  const ipFlag = pi.getFlag("hardware-ip") as string | undefined;
  if (ipFlag) {
    OVERRIDES.esp32Ip = ipFlag;
  }
}
