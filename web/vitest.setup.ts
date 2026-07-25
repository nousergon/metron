import "@testing-library/jest-dom/vitest";

// @tanstack/react-virtual's useWindowVirtualizer creates a ResizeObserver on
// document.documentElement. On suite teardown jsdom disposes `window` before
// those pending callbacks have fired, and the react-dom update triggered by the
// callback reaches getCurrentEventPriority → `window.event` → ReferenceError.
// Intercept the window getter so it falls back to the last-live window object
// if jsdom's own getter has started throwing.
const _win = globalThis.window;
const _desc = Object.getOwnPropertyDescriptor(globalThis, "window");
if (_desc?.get) {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    enumerable: true,
    get() {
      try {
        return _desc.get!.call(globalThis);
      } catch {
        return _win;
      }
    },
    set(v: unknown) {
      _desc.set?.call(globalThis, v);
    },
  });
}
