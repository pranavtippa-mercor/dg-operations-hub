import { readFileSync } from 'node:fs';
import { deepStrictEqual } from 'node:assert/strict';
import { unlock } from '../lib/crypto.ts';
import { validateSnapshot } from '../lib/model.ts';

try {
  const key = process.env.DG_HUB_ACCESS_KEY;
  if (!key) throw new Error('missing key');
  const plain = validateSnapshot(JSON.parse(readFileSync('.private/snapshot.json', 'utf8')));
  const encrypted = JSON.parse(readFileSync('.private/snapshot.enc.json', 'utf8'));
  const decoded = await unlock(encrypted, key);
  deepStrictEqual(decoded, plain);
  console.log('Hosted snapshot validation and browser-compatible decryption passed.');
} catch {
  // Assertions can include complete private objects; never print their details.
  console.error('Hosted snapshot verification failed; private details omitted.');
  process.exitCode = 1;
}
