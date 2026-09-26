import assert from "node:assert/strict";
import test from "node:test";
import { repairSubmissionDecision } from "../repair-loop/integrated.mjs";

test("uncertain repair input never triggers a blind resend", () => {
  assert.equal(repairSubmissionDecision("REPAIR_INPUT_UNKNOWN", false), "wait_without_resend");
  assert.equal(repairSubmissionDecision("REPAIR_INPUT_UNKNOWN", true), "reconcile");
  assert.equal(repairSubmissionDecision("AWAITING_REPAIR_CANDIDATE", false), "wait_without_resend");
});

test("only a planned input with no saved message may be submitted", () => {
  assert.equal(repairSubmissionDecision("REPAIR_INPUT_PLANNED", false), "submit_once");
  assert.equal(repairSubmissionDecision("REPAIR_INPUT_PLANNED", true), "reconcile");
  assert.throws(() => repairSubmissionDecision("VERIFIED", false));
});
