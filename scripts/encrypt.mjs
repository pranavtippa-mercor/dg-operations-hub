import {gzipSync} from 'node:zlib';
import { dirname } from "node:path";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { randomBytes, pbkdf2Sync, createCipheriv } from "node:crypto";
const password = process.env.DG_HUB_ACCESS_KEY;
if (!password || password.length < 32)
  throw new Error("A strong access key is required. Use scripts/publish.py.");
const input = process.argv[2] || ".private/snapshot.json",
  output = process.argv[3] || ".private/snapshot.enc.json";
const body = await readFile(input);
const doc = JSON.parse(body);
if (doc.schema_version !== 1 || !doc.tasks?.length)
  throw new Error("Invalid snapshot");
const salt = randomBytes(16),
  iv = randomBytes(12),
  iterations = 600000;
const key = pbkdf2Sync(password, salt, iterations, 32, "sha256"),
  cipher = createCipheriv("aes-256-gcm", key, iv);
const ciphertext = Buffer.concat([
  cipher.update(gzipSync(body)),
  cipher.final(),
  cipher.getAuthTag(),
]);
await mkdir(dirname(output), { recursive: true });
await writeFile(
  output,
  JSON.stringify({
    format: "dg-operations-encrypted-v1",
    compression: "gzip",
    salt: salt.toString("base64"),
    iv: iv.toString("base64"),
    iterations,
    ciphertext: ciphertext.toString("base64"),
  }),
);
