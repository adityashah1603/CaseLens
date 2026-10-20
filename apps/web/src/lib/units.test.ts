import { describe, expect, it } from "vitest";
import { SseParser } from "../api/sse.ts";
import { checkLinks } from "../features/audit/chain.ts";
import { applyJobUpdate } from "../features/evidence/jobs.ts";
import { forceLayout } from "../features/entities/layout.ts";
import { csvWindow, parseCsv } from "../features/viewer/csv.ts";
import { pdfBoxToViewport, scaleBox } from "../features/viewer/geometry.ts";
import { auditEntries, EV_PDF, EVIDENCE, PLATE_CSV } from "../test/fixtures.ts";
import { formatMs, formatTs } from "./format.ts";
import { roleAtLeast } from "./roles.ts";

describe("geometry", () => {
  it("maps a top-left PDF bbox through a flipped, scaled viewport", () => {
    const s = 2;
    const vp = {
      viewBox: [0, 0, 612, 792],
      width: 612 * s,
      height: 792 * s,
      convertToViewportPoint: (x: number, y: number) => [x * s, (792 - y) * s],
    };
    expect(pdfBoxToViewport([72, 100, 172, 150], vp)).toEqual({ left: 144, top: 200, width: 200, height: 100 });
  });

  it("scales a video bbox from frame pixels to the rendered size", () => {
    expect(scaleBox([100, 50, 300, 150], { width: 1920, height: 1080 }, { width: 960, height: 540 })).toEqual({
      left: 50,
      top: 25,
      width: 100,
      height: 50,
    });
    expect(scaleBox([0, 0, 1, 1], { width: 0, height: 0 }, { width: 10, height: 10 })).toBeNull();
  });
});

describe("csv", () => {
  it("parses quoted fields and CRLF", () => {
    expect(parseCsv('a,b\r\n"x,1","say ""hi"""\r\n')).toEqual([
      ["a", "b"],
      ["x,1", 'say "hi"'],
    ]);
  });

  it("windows around the cited row_id and skips the SYNTHETIC comment", () => {
    const w = csvWindow(PLATE_CSV, 118, 2);
    expect(w.found).toBe(true);
    expect(w.header[0]).toBe("row_id");
    expect(w.rows.map((r) => r.cells[0])).toEqual(["116", "117", "118", "119", "120"]);
    expect(w.rows.filter((r) => r.cited).map((r) => r.cells[1])).toEqual(["SYN4821"]);
    expect(csvWindow(PLATE_CSV, 999).found).toBe(false);
  });
});

describe("SSE parser", () => {
  it("assembles frames across chunks and ignores heartbeats", () => {
    const p = new SseParser();
    expect(p.push(": heartbeat\n\nid: 4")).toEqual([]);
    expect(p.push("2\nevent: job.updated\ndata: {\"a\":")).toEqual([]);
    expect(p.push("1}\n\n")).toEqual([{ id: "42", event: "job.updated", data: '{"a":1}' }]);
  });
});

describe("ingest job updates", () => {
  it("replaces a job in the cached evidence list", () => {
    const ev = EVIDENCE[EV_PDF];
    const job = ev?.ingest_jobs[0];
    if (ev === undefined || job === undefined) throw new Error("fixture");
    const data = { pages: [{ items: [ev], next_cursor: null }], pageParams: [undefined] };
    const next = applyJobUpdate(data, { ...job, status: "running" });
    expect(next?.pages[0]?.items[0]?.ingest_jobs[0]?.status).toBe("running");
    const added = applyJobUpdate(data, { ...job, id: "new-job", stage: "embed", status: "queued" });
    expect(added?.pages[0]?.items[0]?.ingest_jobs).toHaveLength(2);
  });
});

describe("audit chain cross-check", () => {
  it("checks prev_hash against the previous row on adjacent ids", () => {
    expect(checkLinks(auditEntries())).toEqual({ checked: 1, broken: [] });
    const [a, b] = auditEntries();
    if (a === undefined || b === undefined) throw new Error("fixture");
    expect(checkLinks([a, { ...b, prev_hash: "f".repeat(64) }])).toEqual({ checked: 1, broken: [8] });
  });
});

describe("misc", () => {
  it("formats timestamps in their own offset", () => {
    expect(formatTs("2026-03-14T22:40:58-07:00")).toBe("2026-03-14 22:40:58 -07:00");
    expect(formatTs("2026-03-20T09:00:00.123Z")).toBe("2026-03-20 09:00:00 UTC");
    expect(formatMs(41200)).toBe("0:41.2");
    expect(formatMs(3_725_000)).toBe("1:02:05.0");
  });

  it("orders roles", () => {
    expect(roleAtLeast("lead", "analyst")).toBe(true);
    expect(roleAtLeast("viewer", "analyst")).toBe(false);
    expect(roleAtLeast(undefined, "viewer")).toBe(false);
  });

  it("lays out a graph deterministically inside the canvas", () => {
    const a = forceLayout(["x", "y", "z"], [{ source: "x", target: "y" }], 400, 300);
    const b = forceLayout(["x", "y", "z"], [{ source: "x", target: "y" }], 400, 300);
    expect([...a.values()]).toEqual([...b.values()]);
    for (const n of a.values()) {
      expect(n.x).toBeGreaterThanOrEqual(20);
      expect(n.x).toBeLessThanOrEqual(380);
    }
  });
});
