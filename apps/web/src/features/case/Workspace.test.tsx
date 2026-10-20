import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CASE_ID, EV_TAMPERED, insufficientFixture } from "../../test/fixtures.ts";
import { renderApp } from "../../test/render.tsx";
import { mockState } from "../../test/server.ts";
import type { PdfDocumentLike } from "../viewer/pdfjs.ts";

// pdf.js needs a real canvas; the fake page reproduces its viewport math (scale, y flip).
const PAGE_H = 792;
const renders: { page: number; scale: number }[] = [];
vi.mock("../viewer/pdfjs.ts", () => ({
  loadPdf: (): Promise<PdfDocumentLike> =>
    Promise.resolve({
      numPages: 3,
      destroy: () => Promise.resolve(),
      getPage: (n: number) =>
        Promise.resolve({
          getViewport: ({ scale }: { scale: number }) => ({
            scale,
            width: 612 * scale,
            height: PAGE_H * scale,
            viewBox: [0, 0, 612, PAGE_H],
            convertToViewportPoint: (x: number, y: number) => [x * scale, (PAGE_H - y) * scale],
          }),
          render: ({ viewport }: { viewport: { scale: number } }) => {
            renders.push({ page: n, scale: viewport.scale });
            return { promise: Promise.resolve(), cancel: () => undefined };
          },
        }),
    }),
}));

const CASE_PATH = `/cases/${CASE_ID}`;

async function askDemoQuestion() {
  const user = userEvent.setup();
  renderApp(CASE_PATH);
  await user.click(await screen.findByRole("button", { name: "Who was in the gray sedan on March 14?" }));
  await screen.findAllByTestId("claim");
  return user;
}

function firstOf(els: HTMLElement[]): HTMLElement {
  const el = els[0];
  if (el === undefined) throw new Error("no element");
  return el;
}

function viewer() {
  return screen.getByTestId("evidence-viewer");
}

describe("case workspace: ask and open citations", () => {
  beforeEach(() => {
    renders.length = 0;
  });

  it("asks, renders the cited answer and the tool trace", async () => {
    await askDemoQuestion();
    expect(screen.getAllByTestId("claim")).toHaveLength(4);
    expect(screen.getByText(/Agent trace: 1 tool call/)).toBeInTheDocument();
    expect(screen.getByText("SELECT row_id FROM plate_reads WHERE plate = %(plate)s")).toBeInTheDocument();
  });

  it("opens a PDF citation on the cited page with the bbox highlighted", async () => {
    const user = await askDemoQuestion();
    const claim = screen.getAllByTestId("claim")[3];
    if (claim === undefined) throw new Error("claim");
    await user.click(within(claim).getByRole("button", { name: /^Citation 4/ }));
    expect(viewer()).toHaveAttribute("data-locator-kind", "pdf");
    const hl = await screen.findByTestId("pdf-highlight");
    // bbox [72, 410.5, 540, 452] in top-left PDF points at scale 1.25.
    expect(hl.style.left).toBe("90px");
    expect(hl.style.top).toBe("513.125px");
    expect(hl.style.width).toBe("585px");
    expect(hl.style.height).toBe("51.875px");
    expect(screen.getByTestId("pdf-page")).toHaveTextContent("Page 2 of 3");
    expect(renders.some((r) => r.page === 2)).toBe(true);
    expect(within(viewer()).getByText(/around 10:15 I saw a gray car/)).toBeInTheDocument();
  });

  it("opens an audio citation at its start time with the speaker", async () => {
    const user = await askDemoQuestion();
    await user.click(firstOf(screen.getAllByRole("button", { name: /^Citation 3: Audio/ })));
    const audio = await screen.findByTestId("audio-view");
    expect(audio).toHaveAttribute("data-start-ms", "41200");
    expect(audio).toHaveAttribute("data-end-ms", "52900");
    expect(screen.getByTestId("audio-speaker")).toHaveTextContent("SPEAKER_01");
    const el = within(audio).getByLabelText("Cited audio");
    await waitFor(() => {
      expect(el.getAttribute("src")).toMatch(/^blob:synthetic-/);
    });
    expect((el as HTMLAudioElement).currentTime).toBeCloseTo(41.2);
  });

  it("opens a video citation at the frame with the bbox scaled to the rendered size", async () => {
    const user = await askDemoQuestion();
    await user.click(firstOf(screen.getAllByRole("button", { name: /^Citation 2: Video/ })));
    const view = await screen.findByTestId("video-view");
    expect(view).toHaveAttribute("data-start-ms", "125000");
    expect(screen.getByTestId("video-frame")).toHaveTextContent("3750");
    const video = within(view).getByLabelText("Cited video");
    Object.defineProperty(video, "videoWidth", { value: 1280, configurable: true });
    Object.defineProperty(video, "videoHeight", { value: 720, configurable: true });
    Object.defineProperty(video, "clientWidth", { value: 640, configurable: true });
    Object.defineProperty(video, "clientHeight", { value: 360, configurable: true });
    fireEvent.loadedMetadata(video);
    const box = await screen.findByTestId("video-bbox");
    expect(box.style.left).toBe("206px");
    expect(box.style.top).toBe("110px");
    expect(box.style.width).toBe("103px");
    expect(box.style.height).toBe("65.5px");
    expect((video as HTMLVideoElement).currentTime).toBeCloseTo(125);
  });

  it("opens a table-row citation on the cited row with its neighbours", async () => {
    const user = await askDemoQuestion();
    await user.click(firstOf(screen.getAllByRole("button", { name: /^Citation 1: Table row/ })));
    const cited = await screen.findByTestId("csv-cited-row");
    expect(cited).toHaveTextContent("118");
    expect(cited).toHaveTextContent("SYN4821");
    const rows = within(screen.getByTestId("csv-view")).getAllByTestId("csv-row");
    expect(rows).toHaveLength(6);
    expect(screen.getByText("SYN,1005")).toBeInTheDocument();
    expect(screen.queryByText(/SYNTHETIC DATA - fictional/)).not.toBeInTheDocument();
  });

  it("shows the insufficient-evidence state from the API", async () => {
    mockState.answer = insufficientFixture();
    const user = userEvent.setup();
    renderApp(CASE_PATH);
    await user.type(await screen.findByLabelText("Question"), "Who employed the shooter?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByTestId("insufficient")).toBeInTheDocument();
  });
});

describe("evidence viewer integrity failure", () => {
  it("does not show a file whose integrity check failed (409)", async () => {
    const pdfCitation = mockState.answer.citations[3];
    if (pdfCitation === undefined) throw new Error("citation");
    pdfCitation.evidence_id = EV_TAMPERED;
    const user = userEvent.setup();
    renderApp(CASE_PATH);
    await user.click(await screen.findByRole("button", { name: "Who was in the gray sedan on March 14?" }));
    const claim = (await screen.findAllByTestId("claim"))[3];
    if (claim === undefined) throw new Error("claim");
    await user.click(within(claim).getByRole("button", { name: /^Citation 4/ }));
    expect(await screen.findByText("Integrity check failed: this file is not shown.")).toBeInTheDocument();
    expect(screen.queryByTestId("pdf-highlight")).not.toBeInTheDocument();
  });
});

describe("timeline, entities and audit", () => {
  it("timeline sources open the viewer", async () => {
    const user = userEvent.setup();
    renderApp(`${CASE_PATH}?tab=timeline`);
    const ev = await screen.findByTestId("timeline-event");
    expect(ev).toHaveTextContent("2026-03-14 22:40:58 -07:00");
    await user.click(within(ev).getByRole("button", { name: /Open Table row source, plate_reads row 118/ }));
    expect(await screen.findByTestId("csv-cited-row")).toHaveTextContent("SYN4821");
  });

  it("entity detail shows mentions that open the viewer, and the graph renders", async () => {
    const user = userEvent.setup();
    renderApp(`${CASE_PATH}?tab=entities`);
    await user.click(await screen.findByRole("button", { name: /gray sedan/ }));
    const mentions = await screen.findByRole("region", { name: "Mentions" });
    await user.click(within(mentions).getByRole("button", { name: /Open PDF source, p. 2/ }));
    expect(await screen.findByTestId("pdf-highlight")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Graph" }));
    expect(await screen.findByRole("group", { name: "Entity graph" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "SYN4821 (vehicle), 3 mentions" })).toBeInTheDocument();
  });

  it("lead sees the audit log with chain cross-check", async () => {
    const user = userEvent.setup();
    renderApp(CASE_PATH);
    await user.click(await screen.findByRole("tab", { name: "Audit" }));
    expect(await screen.findAllByTestId("audit-row")).toHaveLength(2);
    expect(screen.getByTestId("chain-check")).toHaveTextContent("1 adjacent hash links checked on this page: all consistent.");
  });
});
