import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { resetMockState, server } from "./server.ts";

// jsdom gaps: object URLs, scrolling and media playback are not implemented.
let blobCounter = 0;
URL.createObjectURL = vi.fn(() => `blob:synthetic-${String((blobCounter += 1))}`);
URL.revokeObjectURL = vi.fn();
Element.prototype.scrollIntoView = vi.fn();
HTMLMediaElement.prototype.pause = vi.fn();
HTMLMediaElement.prototype.play = vi.fn(() => Promise.resolve());
HTMLMediaElement.prototype.load = vi.fn();

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  resetMockState();
  sessionStorage.clear();
});

afterAll(() => {
  server.close();
});
