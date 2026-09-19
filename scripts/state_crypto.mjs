#!/usr/bin/env node
// Collector state uses a separate machine key, never the dashboard reader key.
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';
import { constants, closeSync, fstatSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import { gzipSync, gunzipSync } from 'node:zlib';

const MAX_BYTES = 128 * 1024 * 1024;
const FORMAT = 'dg-operations-state';

function base64url(value, length) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]*$/.test(value)) throw new Error('encoding');
  const bytes = Buffer.from(value, 'base64url');
  if (bytes.toString('base64url') !== value || (length !== undefined && bytes.length !== length)) throw new Error('encoding');
  return bytes;
}

function input(path) {
  if (path === '-') return readFileSync(0);
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.size > MAX_BYTES) throw new Error('input');
    return readFileSync(fd);
  } finally { closeSync(fd); }
}

function output(path, bytes) {
  if (path === '-') { process.stdout.write(bytes); return; }
  // Do not follow a symlink or overwrite a pre-existing credential/state file.
  const fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try { writeFileSync(fd, bytes); } finally { closeSync(fd); }
}

function transform(operation, bytes, purpose, key) {
  if (!/^[a-z][a-z0-9._/-]{0,127}$/.test(purpose || '') || bytes.length > MAX_BYTES) throw new Error('arguments');
  const aad = Buffer.from(JSON.stringify({ format: FORMAT, version: 1, purpose, compression: 'gzip' }));
  let result;
  if (operation === 'encrypt') {
    const iv = randomBytes(12);
    const cipher = createCipheriv('aes-256-gcm', key, iv);
    cipher.setAAD(aad);
    const ciphertext = Buffer.concat([cipher.update(gzipSync(bytes)), cipher.final()]);
    result = Buffer.from(JSON.stringify({
      format: FORMAT, version: 1, purpose, compression: 'gzip',
      iv: iv.toString('base64url'), tag: cipher.getAuthTag().toString('base64url'),
      ciphertext: ciphertext.toString('base64url'),
    }));
  } else {
    const envelope = JSON.parse(bytes.toString('utf8'));
    const keys = ['ciphertext', 'compression', 'format', 'iv', 'purpose', 'tag', 'version'];
    if (!envelope || Array.isArray(envelope) || Object.keys(envelope).sort().join() !== keys.join() ||
        envelope.format !== FORMAT || envelope.version !== 1 || envelope.purpose !== purpose || envelope.compression !== 'gzip') throw new Error('envelope');
    const decipher = createDecipheriv('aes-256-gcm', key, base64url(envelope.iv, 12));
    decipher.setAAD(aad);
    decipher.setAuthTag(base64url(envelope.tag, 16));
    const compressed = Buffer.concat([decipher.update(base64url(envelope.ciphertext)), decipher.final()]);
    result = gunzipSync(compressed, { maxOutputLength: MAX_BYTES });
  }
  if (result.length > MAX_BYTES) throw new Error('output');
  return result;
}

try {
  const [operation, source, destination, purpose, extra] = process.argv.slice(2);
  if (!['encrypt', 'decrypt', 'encrypt-batch', 'decrypt-batch'].includes(operation) || !source || !destination || extra) throw new Error('arguments');
  const key = base64url(process.env.DG_HUB_STATE_KEY, 32);
  const bytes = input(source);
  if (bytes.length > MAX_BYTES) throw new Error('input');
  let result;
  if (operation.endsWith('-batch')) {
    const records = JSON.parse(bytes.toString('utf8'));
    if (purpose !== 'batch' || !Array.isArray(records) || records.length > 20000) throw new Error('batch');
    let total = 0;
    const transformed = records.map(record => {
      if (!record || typeof record.id !== 'string' || !/^[A-Za-z0-9._/-]{1,256}$/.test(record.id)) throw new Error('batch');
      const value = transform(operation.replace('-batch', ''), base64url(record.data), record.purpose, key);
      total += value.length;
      if (total > MAX_BYTES) throw new Error('batch');
      return { id: record.id, data: value.toString('base64url') };
    });
    result = Buffer.from(JSON.stringify(transformed));
  } else {
    result = transform(operation, bytes, purpose, key);
  }
  if (result.length > MAX_BYTES) throw new Error('output');
  output(destination, result);
} catch {
  // Never print input, key material, file paths, or provider error details.
  process.stderr.write('Encrypted state operation failed.\n');
  process.exitCode = 1;
}
