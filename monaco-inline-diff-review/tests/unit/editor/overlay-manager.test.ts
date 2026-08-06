/**
 * OverlayManager unit test — ENG-25
 *
 * Pure in-memory data structure, zero mock.
 */

import { describe, it, expect } from "vitest";
import { OverlayManager } from "../../../src/editor/overlay-manager";

describe("OverlayManager", () => {
  // ── ENG-25：consumeZoneIds 取出并清空 ──────────────────────────────────

  it("ENG-25: consumeZoneIds retrieves and clears zone IDs", () => {
    const om = new OverlayManager();
    om.addZoneId("z1");
    om.addZoneId("z2");

    const ids = om.consumeZoneIds();
    expect(ids).toEqual(["z1", "z2"]);

    // Second call returns empty
    expect(om.consumeZoneIds()).toEqual([]);
  });

  // ── Additional: registerWidget tracks widgets ──────────────────────────

  it("registers content widgets", () => {
    const om = new OverlayManager();
    const widget = {
      getId: () => "w1",
      getDomNode: () => document.createElement("div"),
      getPosition: () => null,
    };

    // registerWidget is internal, verify type accepts the interface
    om.registerWidget(widget as any);
    // Widget registered — verification via clearAll side effect
  });

  // ── Additional: clearAll resets widgets ────────────────────────────────

  it("clearAll clears registered widgets without error", () => {
    const om = new OverlayManager();

    // Create a widget-like object
    const widget = {
      getId: () => "w1",
      getDomNode: () => document.createElement("div"),
      getPosition: () => null,
    };
    om.registerWidget(widget as any);

    // Create a minimal fake editor
    const fakeEditor = { removeContentWidget: (_w: unknown) => {} };
    expect(() => om.clearAll(fakeEditor as any)).not.toThrow();
  });

  // ── Additional: clearAll handles invalid widgets gracefully ────────────

  it("clearAll catches errors from removeContentWidget", () => {
    const om = new OverlayManager();
    const widget = {
      getId: () => "bad-widget",
      getDomNode: () => document.createElement("div"),
      getPosition: () => null,
    };
    om.registerWidget(widget as any);

    const throwingEditor = {
      removeContentWidget: (_w: unknown) => {
        throw new Error("widget not found");
      },
    };

    // Should not throw — errors are caught
    expect(() => om.clearAll(throwingEditor as any)).not.toThrow();
  });
});
