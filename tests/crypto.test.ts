import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { isEnvelope, unlock } from "../lib/crypto.ts";
import { randomBytes } from "node:crypto";
test("publisher AES-GCM output decrypts in Web Crypto and rejects wrong keys/tampering", async () => {
  const dir = mkdtempSync(join(tmpdir(), "dg-hub-encryption-"));
  try {
    const time = "2026-09-19T00:00:00Z";
    const data = {
      schema_version: 1,
      generated_at: time,
      mode: "Synthetic test",
      tasks: [
        {
          id: "example-id",
          name: "Synthetic task",
          cohort: "docx",
          artifact: "docx",
          domain: "BUS",
          stage: "In Audit",
          status_id: "audit",
          rfd: false,
          owner: "Example",
          owner_id: null,
          last_actor: "Example",
          updated_at: time,
          transitioned_at: time,
          activity_conflict: false,
          blocked: false,
          deadline: null,
          deadline_history: {},
          slack_activity: null,
          confirmation: null,
          warning: null,
          studio: "https://example.com",
          slack: null,
        },
      ],
      sources: Object.fromEntries(
        ["tasks", "activity", "confirmations", "slack", "modules"].map((k) => [
          k,
          { at: time, cadence_minutes: 5, label: k },
        ]),
      ),
      history: [],
      conflicts: [],
      modules: {
        generated_at_utc: time,
        window_start_local: "2026-09-18 17",
        window_end_local: "2026-09-18 17:00",
        tz: "America/Los_Angeles",
        n_hours: 1,
        hours: [time],
        excluded_other_world: 0,
        modules: [],
      },
    };
    const input = join(dir, "input.json"),
      output = join(dir, "encrypted.json"),
      password = randomBytes(32).toString("base64url");
    writeFileSync(input, JSON.stringify(data));
    execFileSync("node", ["scripts/encrypt.mjs", input, output], {
      env: { ...process.env, DG_HUB_ACCESS_KEY: password },
    });
    const encrypted = JSON.parse(readFileSync(output, "utf8"));
    assert.ok(isEnvelope(encrypted));
    assert.equal(
      readFileSync(output, "utf8").includes("Synthetic task"),
      false,
    );
    assert.deepEqual(await unlock(encrypted, password), data);
    await assert.rejects(unlock(encrypted, "incorrect-key"));
    encrypted.ciphertext = encrypted.ciphertext.slice(0, -5) + "AAAAA";
    await assert.rejects(unlock(encrypted, password));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
