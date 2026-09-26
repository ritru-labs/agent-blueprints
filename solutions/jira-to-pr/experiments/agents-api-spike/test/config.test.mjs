import assert from "node:assert/strict";
import test from "node:test";
import { requireApiKey } from "../config.mjs";

test("live invocation requires a nonempty local API key", () => {
  assert.throws(() => requireApiKey(undefined), /OPENAI_API_KEY is missing/);
  assert.throws(() => requireApiKey("  "), /OPENAI_API_KEY is missing/);
  assert.equal(requireApiKey("local-only-test-value"), "local-only-test-value");
});
