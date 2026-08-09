/**
 * Main-thread client for the image encoder Worker.
 *
 * Lazily boots a single ``imageEncode.worker`` and multiplexes requests onto
 * it by a random request id. Falls back to an inline call when the Worker
 * can't be constructed (tests, ancient browsers) so the Composer always has a
 * working path.
 */
import {
  encodeImageInWorker,
  type EncodeResponse,
} from "./workers/imageEncode.worker";

export type { EncodeResponse, EncodeSuccess, EncodeFailure } from "./workers/imageEncode.worker";

function newId(): string {
  // ``crypto.randomUUID`` is widely available; fall back to Math.random if not.
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return (crypto as Crypto).randomUUID();
  }
  return `img-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** Encode ``file`` off the main thread when possible. Always resolves — errors
 * are returned as ``{ok: false, reason}`` — so callers can render inline
 * validation without wrapping in try/catch. */
export async function encodeImage(file: File): Promise<EncodeResponse> {
  // Worker encoding not available in Next.js; use inline fallback
  const id = newId();
  return encodeImageInWorker({ id, file });
}
