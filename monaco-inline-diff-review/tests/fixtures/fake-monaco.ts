/**
 * Fake Monaco Editor fixtures for unit / component testing.
 *
 * Implements the minimal subset of ITextModel / IStandaloneCodeEditor / monaco
 * needed to test InlineDiffEditor without a real Monaco instance in jsdom.
 *
 * Design: test-design.md §4.2
 */

import { vi } from "vitest";

// ─── Fake Range ────────────────────────────────────────────────────────────

export interface FakeRange {
  startLineNumber: number;
  startColumn: number;
  endLineNumber: number;
  endColumn: number;
}

// ─── Fake Monaco ───────────────────────────────────────────────────────────

// RangeConstructor must be a real class to support `new` operator
class RangeConstructor {
  startLineNumber: number;
  startColumn: number;
  endLineNumber: number;
  endColumn: number;
  constructor(sl: number, sc: number, el: number, ec: number) {
    this.startLineNumber = sl;
    this.startColumn = sc;
    this.endLineNumber = el;
    this.endColumn = ec;
  }
}

export function createFakeMonaco() {
  return {
    Range: RangeConstructor as unknown as new (
      sl: number,
      sc: number,
      el: number,
      ec: number,
    ) => FakeRange,
    editor: {
      OverviewRulerLane: {
        Right: 4,
      },
    },
  };
}

// ─── Fake Model ────────────────────────────────────────────────────────────

export interface FakeModel {
  _lines: string[];
  getValue: () => string;
  getLineCount: () => number;
  getLineMaxColumn: (lineNumber: number) => number;
  getFullModelRange: () => FakeRange;
  setValue: (text: string) => void;
  pushEditOperations: (
    _selection: unknown,
    operations: Array<{
      range: FakeRange;
      text: string;
      forceMoveMarkers?: boolean;
    }>,
  ) => void;
}

export function createFakeModel(initialValue: string): FakeModel {
  const _lines: string[] =
    initialValue === "" ? [] : initialValue.split("\n");

  return {
    _lines,

    getValue(): string {
      return _lines.join("\n");
    },

    getLineCount(): number {
      return _lines.length === 0 ? 1 : _lines.length;
    },

    getLineMaxColumn(lineNumber: number): number {
      if (_lines.length === 0) return 1;
      const idx = lineNumber - 1;
      if (idx < 0 || idx >= _lines.length) return 1;
      return _lines[idx].length + 1;
    },

    getFullModelRange(): FakeRange {
      const lineCount = this.getLineCount();
      const lastCol = this.getLineMaxColumn(lineCount);
      return { startLineNumber: 1, startColumn: 1, endLineNumber: lineCount, endColumn: lastCol };
    },

    setValue(text: string): void {
      _lines.length = 0;
      if (text !== "") {
        _lines.push(...text.split("\n"));
      }
    },

    pushEditOperations(
      _selection: unknown,
      operations: Array<{
        range: FakeRange;
        text: string;
        forceMoveMarkers?: boolean;
      }>,
    ): void {
      for (const op of operations) {
        const { range, text } = op;
        const startIdx = range.startLineNumber - 1;
        const endIdx = range.endLineNumber - 1;

        // Extract text being inserted
        const insertLines = text === "" ? [] : text.split("\n");

        // ── Zero-width range (pure insertion at column) ──────────────
        if (startIdx === endIdx && range.startColumn === range.endColumn) {
          const col = range.startColumn - 1; // 0-based column
          const currentLine = _lines[startIdx] ?? "";
          const prefix = currentLine.slice(0, col);
          const suffix = currentLine.slice(col);
          _lines.splice(startIdx, 1); // remove current line

          if (insertLines.length === 0) {
            // Deleting at column — merge prefix + suffix
            const merged = prefix + suffix;
            if (merged !== "") _lines.splice(startIdx, 0, merged);
          } else if (insertLines.length === 1) {
            // Single-line insert at column
            const merged = prefix + insertLines[0] + suffix;
            _lines.splice(startIdx, 0, merged);
          } else {
            // Multi-line insert at column
            const first = prefix + insertLines[0];
            const last = insertLines[insertLines.length - 1] + suffix;
            const middle = insertLines.slice(1, -1);
            _lines.splice(startIdx, 0, first, ...middle, last);
          }
          continue;
        }

        // ── Range spans lines — remove [startIdx, endIdx) ──────────
        const removed = _lines.splice(startIdx, endIdx - startIdx);

        // Handle partial-line edits:
        // If startColumn > 1, preserve prefix of first removed line
        // If endColumn > 1 AND within line bounds, preserve suffix of last removed line
        // (endColumn=1 on a subsequent line means the entire last removed line is consumed)
        const prefix =
          removed.length > 0 && range.startColumn > 1
            ? removed[0].slice(0, range.startColumn - 1)
            : "";
        const suffix =
          removed.length > 0 &&
          range.endColumn > 1 &&
          range.endColumn <= (removed[removed.length - 1]?.length ?? 0) + 1
            ? (removed[removed.length - 1] ?? "").slice(range.endColumn - 1)
            : "";

        // Build replacement lines
        if (insertLines.length === 0) {
          // Deleting — just join prefix + suffix
          const merged = prefix + suffix;
          if (merged !== "") {
            _lines.splice(startIdx, 0, merged);
          }
        } else {
          // Insert / Replace
          const firstLine = prefix + insertLines[0];
          const lastLine = (insertLines[insertLines.length - 1] ?? "") + suffix;

          const replacementLines: string[] = [firstLine];
          for (let i = 1; i < insertLines.length - 1; i++) {
            replacementLines.push(insertLines[i]);
          }
          if (insertLines.length > 1) {
            replacementLines.push(lastLine);
          }

          // If only one line and it's empty, remove instead
          if (replacementLines.length === 1 && replacementLines[0] === "") {
            // nothing to insert
          } else {
            _lines.splice(startIdx, 0, ...replacementLines);
          }
        }
      }
    },
  };
}

// ─── Fake Accessor ─────────────────────────────────────────────────────────

export interface FakeAccessor {
  _zones: Array<{ id: string; afterLineNumber: number; domNode: HTMLElement }>;
  _container: HTMLElement | null;
  addZone: (opts: { afterLineNumber: number; heightInLines?: number; domNode: HTMLElement }) => string;
  removeZone: (id: string) => boolean;
  layoutZone: (id: string) => void;
}

export function createFakeAccessor(container?: HTMLElement): FakeAccessor {
  let counter = 0;
  const _zones: Array<{ id: string; afterLineNumber: number; domNode: HTMLElement }> = [];

  return {
    _zones,
    _container: container ?? null,

    addZone(opts: { afterLineNumber: number; heightInLines?: number; domNode: HTMLElement }): string {
      const id = `zone-${counter++}`;
      _zones.push({ id, afterLineNumber: opts.afterLineNumber, domNode: opts.domNode });
      // Attach domNode to the container so querySelectorAll works in tests
      if (container) {
        container.appendChild(opts.domNode);
      }
      return id;
    },

    removeZone(_id: string): boolean {
      const idx = _zones.findIndex((z) => z.id === _id);
      if (idx !== -1) {
        const zone = _zones[idx];
        // Remove from DOM
        if (zone.domNode.parentNode) {
          zone.domNode.parentNode.removeChild(zone.domNode);
        }
        _zones.splice(idx, 1);
        return true;
      }
      return false;
    },

    layoutZone(_id: string): void {
      // no-op in fake
    },
  };
}

// ─── Fake Deco Collection ──────────────────────────────────────────────────

export interface FakeDecoCollection {
  _lastDecos: unknown[];
  set: (decos: unknown[]) => void;
  clear: () => void;
}

export function createFakeDecoCollection(): FakeDecoCollection {
  return {
    _lastDecos: [],
    set(decos: unknown[]): void {
      this._lastDecos = decos;
    },
    clear(): void {
      this._lastDecos = [];
    },
  };
}

// ─── Fake Editor ───────────────────────────────────────────────────────────

export interface FakeEditor {
  _model: FakeModel;
  _accessor: FakeAccessor;
  _decoCol: FakeDecoCollection;
  _viewZonesChanged: boolean;
  getModel: () => FakeModel;
  changeViewZones: (cb: (accessor: FakeAccessor) => void) => void;
  createDecorationsCollection: () => FakeDecoCollection;
  pushUndoStop: () => void;
  removeContentWidget: (_widget: unknown) => void;
  revealLineInCenter: (_lineNumber: number) => void;
}

export function createFakeEditor(
  initialValue: string,
  container?: HTMLElement,
): FakeEditor {
  const _model = createFakeModel(initialValue);
  const _accessor = createFakeAccessor(container);
  const _decoCol = createFakeDecoCollection();

  return {
    _model,
    _accessor,
    _decoCol,
    _viewZonesChanged: false,

    getModel(): FakeModel {
      return _model;
    },

    changeViewZones(cb: (accessor: FakeAccessor) => void): void {
      this._viewZonesChanged = true;
      cb(_accessor);
    },

    createDecorationsCollection(): FakeDecoCollection {
      return _decoCol;
    },

    pushUndoStop(): void {
      // no-op
    },

    removeContentWidget(_widget: unknown): void {
      // no-op
    },

    revealLineInCenter(_lineNumber: number): void {
      // no-op
    },
  };
}

// ─── Spies (for assertions) ────────────────────────────────────────────────

/** Create a FakeModel with all methods spied on via vi.fn() wrappers. */
export function createSpiedFakeModel(initialValue: string): FakeModel {
  const model = createFakeModel(initialValue);
  return {
    _lines: model._lines,
    getValue: vi.fn(model.getValue.bind(model)),
    getLineCount: vi.fn(model.getLineCount.bind(model)),
    getLineMaxColumn: vi.fn(model.getLineMaxColumn.bind(model)),
    getFullModelRange: vi.fn(model.getFullModelRange.bind(model)),
    setValue: vi.fn(model.setValue.bind(model)),
    pushEditOperations: vi.fn(model.pushEditOperations.bind(model)),
  };
}

/** Create a FakeEditor wrapping a SpiedFakeModel, with key methods spied. */
export function createSpiedFakeEditor(
  initialValue: string,
  container?: HTMLElement,
): FakeEditor & {
  changeViewZones: ReturnType<typeof vi.fn>;
} {
  const model = createSpiedFakeModel(initialValue);
  const accessor = createFakeAccessor(container);
  const decoCol = createFakeDecoCollection();

  return {
    _model: model,
    _accessor: accessor,
    _decoCol: decoCol,
    _viewZonesChanged: false,

    getModel: vi.fn(() => model),
    changeViewZones: vi.fn((cb: (a: FakeAccessor) => void) => {
      (accessor as unknown as Record<string, unknown>)._viewZonesChanged = true;
      cb(accessor);
    }),
    createDecorationsCollection: vi.fn(() => decoCol),
    pushUndoStop: vi.fn(),
    removeContentWidget: vi.fn(),
    revealLineInCenter: vi.fn(),
  } as unknown as FakeEditor & { changeViewZones: ReturnType<typeof vi.fn> };
}
