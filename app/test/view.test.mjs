/**
 * Tests for the two rules the Results view must not get wrong: which test it
 * shows, and what it draws when it cannot source an instant.
 *
 * Both are places where a screen can quietly lie -- by choosing the test that
 * flatters the run, or by putting a marker where a measurement should be.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { chooseFocus, margin } from "../src/app/lib/focus.mjs";
import { readTimeline, ms, hex } from "../src/app/lib/timeline.mjs";

const pass = (test, latency_us, budget_ms) => ({
  test,
  outcome: "pass",
  latency_us,
  budget_ms,
});

describe("which test the timeline shows", () => {
  it("shows a failure ahead of every passing test", () => {
    const chosen = chooseFocus([
      pass("a", 100, 50),
      { test: "b", outcome: "fail", latency_us: null, budget_ms: 50 },
      pass("c", 49000, 50),
    ]);
    assert.equal(chosen.test, "b");
    assert.match(chosen.why, /failure/);
  });

  it("shows the closest call when everything passed", () => {
    // NOT the fastest. Picking the fastest would flatter the run, which is the
    // opposite of this product's job.
    const chosen = chooseFocus([
      pass("fast", 400, 50),
      pass("close", 45000, 50),
      pass("middling", 20000, 50),
    ]);
    assert.equal(chosen.test, "close");
    assert.match(chosen.why, /closest call/);
    assert.match(chosen.why, /90\.0%/);
  });

  it("compares margins, not raw latencies", () => {
    // 40 ms of a 50 ms budget is a closer call than 100 ms of a 1000 ms one,
    // even though the second number is larger.
    const chosen = chooseFocus([pass("slow", 100000, 1000), pass("tight", 40000, 50)]);
    assert.equal(chosen.test, "tight");
  });

  it("does not claim more than it established when nothing is comparable", () => {
    // The wording used to be "no test in this run has a timing budget" -- a
    // claim about the records that this function never checks. All it knows is
    // that no test had BOTH a latency and a budget to compare.
    const chosen = chooseFocus([pass("a", null, null), pass("b", null, null)]);
    assert.equal(chosen.test, "a");
    assert.match(chosen.why, /both a latency and a budget/);
  });

  it("shows a test that reached no verdict, and does not call it a failure", () => {
    // refused, timeout, crashed and inconsistent mean no verdict was reached.
    // Announcing one as "the first failure" reports firmware that broke.
    const chosen = chooseFocus([
      pass("a", 400, 50),
      { test: "b", outcome: "crashed", latency_us: null, budget_ms: null },
    ]);
    assert.equal(chosen.test, "b");
    assert.doesNotMatch(chosen.why, /failure/);
    assert.match(chosen.why, /no verdict/);
    assert.match(chosen.why, /crashed/);
  });

  it("still prefers a real failure over a test that reached no verdict", () => {
    const chosen = chooseFocus([
      { test: "crash", outcome: "timeout", latency_us: null, budget_ms: null },
      { test: "real", outcome: "fail", latency_us: null, budget_ms: 50 },
    ]);
    assert.equal(chosen.test, "real");
    assert.match(chosen.why, /failure/);
  });

  it("returns nothing for an empty run", () => {
    assert.equal(chooseFocus([]), null);
    assert.equal(chooseFocus(null), null);
  });

  it("has no margin without both a latency and a budget", () => {
    assert.equal(margin({ latency_us: 400, budget_ms: null }), null);
    assert.equal(margin({ latency_us: null, budget_ms: 50 }), null);
    assert.equal(margin({ latency_us: 400, budget_ms: 50 }), 0.008);
  });
});

describe("reading the three instants", () => {
  const record = {
    latency: { headline_token: "t3", budget_ms: 50, headline_us: 400 },
    stimuli: [
      { us: 100, what: "write_symbol", detail: "node.sym=1@0x2000:4" },
      { us: 600000, what: "write_symbol", detail: "node.sym=600@0x2000:4" },
      { us: 900000, what: "write_symbol", detail: "node.sym=250@0x2000:4" },
    ],
    assertions: [
      { token: "t1", armed_us: 0, met_us: 200, verdict: "PASS" },
      {
        token: "t3",
        armed_us: 600000,
        met_us: 600400,
        latency_us: 400,
        window_ms: 50,
        verdict: "PASS",
        matched_frame: { id: 1540, us: 600400 },
        detail: { signals: { code: "SOMETHING" } },
      },
    ],
  };

  it("finds injected, reacted and the deadline", () => {
    const tl = readTimeline(record);
    assert.equal(tl.injectedUs, 600000);
    assert.equal(tl.reactedUs, 600400);
    assert.equal(tl.deadlineUs, 600000 + 50 * 1000);
    assert.equal(tl.latencyUs, 400);
  });

  it("attributes the stimulus that caused the arming, not the last one", () => {
    // The stimulus at 900 ms happened AFTER this assertion armed, so it cannot
    // be what provoked it. Same ordering as the engine's causation invariant.
    const tl = readTimeline(record);
    assert.equal(tl.cause.us, 600000);
    assert.match(tl.cause.detail, /=600/);
  });

  it("returns null rather than drawing a timeline it cannot source", () => {
    assert.equal(readTimeline(null), null);
    assert.equal(readTimeline({ latency: {}, assertions: [] }), null);
    assert.equal(
      readTimeline({
        latency: { headline_token: "tX" },
        assertions: [{ token: "t1", armed_us: 0 }],
      }),
      null
    );
  });

  it("reports a missing reaction as absent, never as zero", () => {
    const noReaction = {
      latency: { headline_token: "t1", budget_ms: 50 },
      stimuli: [],
      assertions: [{ token: "t1", armed_us: 1000, met_us: null, window_ms: 50 }],
    };
    const tl = readTimeline(noReaction);
    assert.equal(tl.reactedUs, null);
    assert.equal(tl.deadlineUs, 1000 + 50000);
  });

  it("has no deadline when nothing declared a budget", () => {
    const tl = readTimeline({
      latency: { headline_token: "t1" },
      assertions: [{ token: "t1", armed_us: 0, met_us: 5 }],
    });
    assert.equal(tl.deadlineUs, null);
    assert.equal(tl.budgetMs, null);
  });
});

describe("formatting", () => {
  it("keeps microsecond precision", () => {
    assert.equal(ms(400), "0.400");
    assert.equal(ms(200400), "200.400");
  });

  it("uses the same decimal count for every number, including round ones", () => {
    // This asserted "50.0" -- one decimal for a whole millisecond, three
    // otherwise. That was wrong, and the reason is the spec: tabular figures
    // exist so a column of latencies can be scanned, and a varying decimal
    // count stops them lining up on the decimal point. The engine measures in
    // microseconds regardless, so the trailing zeros are real precision.
    assert.equal(ms(50000), "50.000");
    assert.equal(ms(0), "0.000");
    assert.equal([ms(400), ms(50000)].every((x) => x.split(".")[1].length === 3), true);
  });

  it("says nothing for an absent measurement", () => {
    assert.equal(ms(null), null);
    assert.equal(ms(undefined), null);
  });

  it("renders identifiers the way the bus does", () => {
    assert.equal(hex(1540), "0x604");
    assert.equal(hex(0x18), "0x018");
    assert.equal(hex(null), null);
  });
});
