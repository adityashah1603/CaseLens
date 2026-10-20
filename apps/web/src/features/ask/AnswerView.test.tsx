import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { AnswerContract } from "../../api/types.ts";
import { answerFixture, insufficientFixture } from "../../test/fixtures.ts";
import { ViewerContext, type ViewerState } from "../viewer/ViewerContext.ts";
import { AnswerView } from "./AnswerView.tsx";
import { resolveAnswer } from "./resolve.ts";

function renderAnswer(answer: AnswerContract) {
  const open = vi.fn();
  const viewer: ViewerState = { target: null, open, close: vi.fn() };
  render(
    <ViewerContext.Provider value={viewer}>
      <AnswerView answer={answer} />
    </ViewerContext.Provider>,
  );
  return { open };
}

describe("AnswerView", () => {
  it("renders every claim with numbered citation links", () => {
    renderAnswer(answerFixture());
    const claims = screen.getAllByTestId("claim");
    expect(claims).toHaveLength(4);
    for (const claim of claims) {
      expect(within(claim).getAllByRole("button", { name: /^Citation \d+:/ }).length).toBeGreaterThan(0);
    }
    const last = claims[3];
    if (last === undefined) throw new Error("missing claim");
    expect(within(last).getByRole("button", { name: "Citation 4: PDF, p. 2" })).toBeInTheDocument();
    expect(within(last).getByRole("button", { name: "Citation 1: Table row, plate_reads row 118" })).toBeInTheDocument();
  });

  it("never shows a claim whose citations do not resolve", () => {
    const answer = answerFixture();
    answer.claims.push({ id: "cl9", text: "An unsupported statement.", citation_ids: ["c99"] });
    renderAnswer(answer);
    expect(screen.queryByText("An unsupported statement.")).not.toBeInTheDocument();
    expect(screen.getByText(/Statement withheld/)).toHaveTextContent("c99");
  });

  it("refuses to show an answered status with no resolvable claim", () => {
    const answer = answerFixture();
    answer.claims = [{ id: "cl1", text: "Uncited prose.", citation_ids: ["c42"] }];
    renderAnswer(answer);
    expect(screen.queryByText("Uncited prose.")).not.toBeInTheDocument();
    expect(screen.queryByText(answer.answer)).not.toBeInTheDocument();
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
  });

  it("renders conflicts side by side with citations on each side", () => {
    renderAnswer(answerFixture());
    const conflict = screen.getByTestId("conflict");
    expect(conflict).toHaveTextContent("Arrival time of the gray sedan disagrees.");
    const sides = within(conflict).getAllByTestId("conflict-side");
    expect(sides).toHaveLength(2);
    const [a, b] = sides;
    if (a === undefined || b === undefined) throw new Error("missing side");
    expect(a).toHaveTextContent("around 22:15");
    expect(within(a).getAllByRole("button", { name: /^Citation/ })).toHaveLength(1);
    expect(b).toHaveTextContent("at 22:41");
    expect(within(b).getAllByRole("button", { name: /^Citation/ })).toHaveLength(2);
    expect(sides[0]?.parentElement).toHaveClass("sm:grid-cols-2");
  });

  it("shows the insufficient-evidence state without claims", () => {
    renderAnswer(insufficientFixture());
    expect(screen.getByText("Insufficient evidence")).toBeInTheDocument();
    expect(screen.getByTestId("insufficient")).toHaveTextContent("no source in this case names");
    expect(screen.queryAllByTestId("claim")).toHaveLength(0);
    expect(screen.getByText("No employment records are in the case file.")).toBeInTheDocument();
  });

  it("shows confidence and open questions", () => {
    renderAnswer(answerFixture());
    expect(screen.getByRole("meter", { name: "Answer confidence" })).toHaveAttribute("aria-valuenow", "78");
    expect(screen.getByText("The passenger is not identified in any cited source.")).toBeInTheDocument();
  });

  it("opens the viewer with the citation's evidence and locator", async () => {
    const user = userEvent.setup();
    const { open } = renderAnswer(answerFixture());
    const claims = screen.getAllByTestId("claim");
    const claim3 = claims[2];
    if (claim3 === undefined) throw new Error("missing claim");
    await user.click(within(claim3).getByRole("button", { name: /^Citation 3/ }));
    expect(open).toHaveBeenCalledWith(
      expect.objectContaining({
        evidenceId: "00000000-0000-4000-8000-0000000000e2",
        locator: { kind: "audio", start_ms: 41200, end_ms: 52900, speaker: "SPEAKER_01" },
      }),
    );
  });
});

describe("resolveAnswer", () => {
  it("numbers citations by their position and flags missing ids", () => {
    const answer = answerFixture();
    answer.claims.push({ id: "cl5", text: "x", citation_ids: ["c2", "c77"] });
    const r = resolveAnswer(answer);
    expect(r.byId.get("c3")?.n).toBe(3);
    const last = r.claims.at(-1);
    expect(last?.refs.ok).toBe(false);
    expect(last?.refs.missing).toEqual(["c77"]);
    expect(r.claims.slice(0, 4).every((c) => c.refs.ok)).toBe(true);
    expect(r.unsupported).toBe(false);
  });
});
