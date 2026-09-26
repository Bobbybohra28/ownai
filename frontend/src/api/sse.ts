// Server-Sent Events over fetch (EventSource cannot send Authorization headers).
// Parses the SSE wire format, resumes with Last-Event-ID after network drops, and
// stops after terminal events (run_finished / run_paused).

import { API_BASE, getAccessToken, refreshSession } from "./client";

export interface RunEvent {
  id: number;
  type: string;
  data: any;
}

const TERMINAL = new Set(["run_finished", "run_paused"]);

export function parseSSE(buffer: string): { events: RunEvent[]; rest: string } {
  const events: RunEvent[] = [];
  const blocks = buffer.split(/\r?\n\r?\n/);
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let id = 0;
    let type = "message";
    const data: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith(":")) continue;
      const idx = line.indexOf(":");
      const field = idx === -1 ? line : line.slice(0, idx);
      const value = idx === -1 ? "" : line.slice(idx + 1).replace(/^ /, "");
      if (field === "id") id = Number(value) || 0;
      else if (field === "event") type = value;
      else if (field === "data") data.push(value);
    }
    if (data.length) {
      try {
        events.push({ id, type, data: JSON.parse(data.join("\n")) });
      } catch {
        events.push({ id, type: "error", data: { code: "FRONTEND_RESPONSE_ERROR", message: "Malformed event from server." } });
      }
    }
  }
  return { events, rest };
}

export function streamRun(runId: string, onEvent: (event: RunEvent) => void, signal: AbortSignal): Promise<void> {
  let lastId = 0;
  let attempts = 0;

  const connect = async (): Promise<void> => {
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    if (lastId) headers["Last-Event-ID"] = String(lastId);
    const response = await fetch(`${API_BASE}/runs/${runId}/events`, { headers, signal, credentials: "include" });
    if (response.status === 401 && (await refreshSession())) return connect();
    if (!response.ok || !response.body) throw new Error(`Event stream failed (HTTP ${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSSE(buffer);
      buffer = parsed.rest;
      for (const event of parsed.events) {
        if (event.id) lastId = Math.max(lastId, event.id);
        attempts = 0;
        onEvent(event);
        if (TERMINAL.has(event.type)) {
          await reader.cancel().catch(() => undefined);
          throw new StopStream();
        }
      }
    }
  };

  const loop = async () => {
    while (!signal.aborted) {
      try {
        await connect();
      } catch (error) {
        if (error instanceof StopStream || signal.aborted) return;
        attempts += 1;
        if (attempts > 8) {
          onEvent({ id: 0, type: "error", data: { code: "FRONTEND_RESPONSE_ERROR", message: "Lost connection to the progress stream." } });
          return;
        }
      }
      await new Promise((r) => setTimeout(r, Math.min(1000 * attempts, 8000)));
    }
  };
  return loop();
}

class StopStream extends Error {}
