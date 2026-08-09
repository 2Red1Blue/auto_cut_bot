// Stub worker exports for imageEncode.ts compatibility
export interface EncodeRequest {
  id: string;
  image: Blob;
  format?: "image/webp" | "image/png" | "image/jpeg";
  quality?: number;
  maxWidth?: number;
  maxHeight?: number;
}

export interface EncodeSuccess {
  id: string;
  status: "ok";
  blob: Blob;
  width: number;
  height: number;
}

export interface EncodeFailure {
  id: string;
  status: "error";
  error: string;
}

export type EncodeResponse = EncodeSuccess | EncodeFailure;

// Stub: inline encoding fallback (no worker in Next.js)
export async function encodeImageInWorker(
  req: { id: string; file: File; quality?: number; maxWidth?: number; maxHeight?: number }
): Promise<EncodeResponse> {
  return {
    id: req.id,
    status: "error",
    error: "Worker encoding not available in Next.js; use direct encoding",
  };
}