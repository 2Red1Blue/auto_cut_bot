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
  private url: string = "";
  private shouldReconnect = true;

  /** Emit an internal event to all registered handlers. */
  private emit(type: string, data?: unknown): void {
    const handlers = this.handlers.get(type);
    if (handlers) {
      handlers.forEach((fn) => fn(data));
    }
  }

  connect(url: string = WS_URL): void {
    this.url = url;
    this.shouldReconnect = true;
    this._connect();
  }

  private _connect(): void {
    // Close any existing connection before opening a new one
    if (this.ws) {
      this.ws.onclose = null; // prevent triggering reconnect on explicit close
      this.ws.close();
      this.ws = null;
    }

    if (this.ws?.readyState === WebSocket.OPEN) return;

    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      this.emit("ws:open");
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
      this.emit("ws:close");
    };

    this.ws.onerror = () => {
      // Error events are followed by onclose, so no need to emit separately
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
    if (this.ws) {
      this.ws.onclose = null; // prevent auto-reconnect
      this.ws.close();
      this.ws = null;
      this.emit("ws:close");
    }
  }

  get readyState(): number {
    return this.ws?.readyState ?? WebSocket.CLOSED;
  }
}

export const wsClient = new WsClient();