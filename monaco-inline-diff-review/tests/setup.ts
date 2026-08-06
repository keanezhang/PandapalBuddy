/**
 * Global test setup for vitest + jsdom.
 *
 * - vitest fake timers to control requestAnimationFrame / scheduleRebuild
 * - jsdom environment for DOM-based tests (zone-builders)
 */

import { beforeAll, afterAll, beforeEach, afterEach, vi } from "vitest";

beforeAll(() => {
  // Ensure jsdom environment is active
});

afterAll(() => {
  // Cleanup
});

beforeEach(() => {
  // Use fake timers so we can control RAF-based scheduleRebuild
  vi.useFakeTimers();
});

afterEach(() => {
  // Restore real timers to avoid leaking fake timers across test suites
  vi.useRealTimers();
  vi.restoreAllMocks();
});
