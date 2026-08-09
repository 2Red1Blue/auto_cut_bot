const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8765/ws";

type MessageHandler = (data: unknown) => void;

interface WsMessage {
  type: string;
  payload?: unknown;
  [key: string]: unknown;
}

class WsClient {
  private ws: WebSocket | null = null;
  private handlers: Map<string, Set<MessageHandler>> = new Map();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private url: string = "";
  private shouldReconnect = true;

  connect(url: string = WS_URL): void {
    this.url = url;
    this.shouldReconnect = true;
    this._connect();
  }

  private _connect(): void {
    if (this.ws?.readyState === WebSocket.OPEN) return;

    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      console.log("[WS] Connected:", this.url);
    };

    this.ws.onmessage = (event) => {
      try {
        const data: WsMessage = JSON.parse(event.data);
        const handlers = this.handlers.get(data.type);
        if (handlers) {
          handlers.forEach((fn) => fn(data.payload ?? data));
        }
        // Also notify "*" handlers for all messages
        const allHandlers = this.handlers.get("*");
        if (allHandlers) {
          allHandlers.forEach((fn) => fn(data));
        }
      } catch {
        // Ignore non-JSON messages
      }
    };

    this.ws.onclose = () => {
      console.log("[WS] Disconnected");
      if (this.shouldReconnect) {
        this.reconnectTimer = setTimeout(() => this._connect(), 3000);
      }
    };

    this.ws.onerror = (err) => {
      console.error("[WS] Error:", err);
    };
  }

  on(type: string, handler: MessageHandler): () => void {
    if (!this.handlers.has(type)) {
      this.handlers.set(type, new Set());
    }
    this.handlers.get(type)!.add(handler);
    return () => {
      this.handlers.get(type)?.delete(handler);
    };
  }

  send(data: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  disconnect(): void {
    this.shouldReconnect = false;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  get readyState(): number {
    return this.ws?.readyState ?? WebSocket.CLOSED;
  }
}

export const wsClient = new WsClient();