import type { ChatMessage, ChatResponse } from "./types";

// The agent is reached directly from the browser. Rather than baking a
// per-environment URL into the image at build time, derive it at runtime from
// the page the app is served on, so ONE image works in every environment:
//
//   - same hostname as the page (dev and prod share the worker node's IP), and
//   - agent NodePort = frontend NodePort + 500  (dev 30300->30800, prod 31300->31800).
//
// Falls back to localhost for `next dev` / SSR where window is unavailable.
function resolveAgentUrl(): string {
  if (typeof window !== "undefined") {
    const { protocol, hostname, port } = window.location;
    // Frontend served on a NodePort -> agent is on that port + 500 on the same host.
    const frontendPort = Number(port);
    if (Number.isFinite(frontendPort) && frontendPort > 0) {
      return `${protocol}//${hostname}:${frontendPort + 500}`;
    }
    // No explicit port (e.g. behind a proxy on 80/443): assume same origin.
    return `${protocol}//${hostname}`;
  }
  return "http://localhost:8000";
}

export async function sendMessage(messages: ChatMessage[]): Promise<ChatResponse> {
  const res = await fetch(`${resolveAgentUrl()}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(text || res.statusText);
  }
  const data = await res.json();
  return data as ChatResponse;
}
