import { validateSnapshot, type Snapshot } from "./model.ts";
export type Envelope = {
  format: "dg-operations-encrypted-v1";
  compression: "gzip";
  salt: string;
  iv: string;
  iterations: number;
  ciphertext: string;
};
const bytes = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
export function isEnvelope(x: unknown): x is Envelope {
  return (
    !!x &&
    typeof x === "object" &&
    "format" in x &&
    x.format === "dg-operations-encrypted-v1"
  );
}
export async function unlock(e: Envelope, password: string): Promise<Snapshot> {
  if (e.iterations !== 600000 || e.compression !== "gzip") throw Error("Unsupported encryption settings.");
  try {
    const material = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(password),
      "PBKDF2",
      false,
      ["deriveKey"],
    );
    const key = await crypto.subtle.deriveKey(
      {
        name: "PBKDF2",
        salt: bytes(e.salt),
        iterations: e.iterations,
        hash: "SHA-256",
      },
      material,
      { name: "AES-GCM", length: 256 },
      false,
      ["decrypt"],
    );
    const decoded = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: bytes(e.iv) },
      key,
      bytes(e.ciphertext),
    );
    const text = await new Response(new Blob([decoded]).stream().pipeThrough(new DecompressionStream("gzip"))).text();
    return validateSnapshot(JSON.parse(text));
  } catch {
    throw Error("Unable to unlock. Check your access key and try again.");
  }
}
