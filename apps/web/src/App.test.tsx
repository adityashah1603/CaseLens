import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import App from "./App.tsx";

describe("App", () => {
  it("renders the SYNTHETIC DATA banner", () => {
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("SYNTHETIC DATA");
  });

  it("renders the CaseLens heading", () => {
    render(<App />);
    expect(screen.getByRole("heading", { level: 1, name: "CaseLens" })).toBeInTheDocument();
  });
});
