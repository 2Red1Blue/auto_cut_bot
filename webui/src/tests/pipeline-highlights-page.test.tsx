import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchPipelineHighlights } from "@/lib/api";
import { PipelineHighlightsPage } from "@/components/pipeline/PipelineHighlightsPage";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchPipelineHighlights: vi.fn(),
}));

const runId = `pipeline_run_${"a".repeat(32)}`;
const fetchHighlightsMock = vi.mocked(fetchPipelineHighlights);

const item = {
  episode_index: 2,
  candidate_id: "candidate-1",
  reason: "A decisive reveal advances the story.",
  anchor_summary: "The character finds the proof.",
  payoff_summary: "The conflict changes direction.",
  dialogue_excerpt: "We finally know the truth.",
  tags: ["reveal"],
  narrative_functions: ["turning_point"],
  editing_modes: ["dialogue"],
  measurements: [{ kind: "dramatic_shift", value: "0.8", confidence: "0.7" }],
  support_confidence: "0.9",
  semantic_window: {
    start_tick: 120,
    end_tick: 180,
    source_time_base: { numerator: 1, denominator: 25 },
    mapping_error_bound_source_ticks: 2,
    provider_uncertainty_proxy_ticks: 3,
    provider_uncertainty_proxy_time_base: { numerator: 1, denominator: 25 },
    precision: "coarse_only" as const,
  },
};

describe("PipelineHighlightsPage", () => {
  beforeEach(() => {
    fetchHighlightsMock.mockReset();
  });

  it("renders committed candidates as coarse semantic windows only", async () => {
    fetchHighlightsMock.mockResolvedValue({ status: "ready", items: [item] });

    render(<PipelineHighlightsPage runId={runId} token="tok" onBackToChat={vi.fn()} />);

    expect(await screen.findByText(item.reason)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Semantic window (coarse)" })).toHaveTextContent(
      "120–180 ticks",
    );
    expect(screen.getByText("Support confidence")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /render|export|publish|edit|cut/i })).not.toBeInTheDocument();
  });

  it("keeps not-ready distinct from a committed empty result", async () => {
    fetchHighlightsMock.mockResolvedValueOnce({ status: "not_ready" });
    const { rerender } = render(
      <PipelineHighlightsPage runId={runId} token="tok" onBackToChat={vi.fn()} />,
    );
    expect(await screen.findByText(/not ready yet/i)).toBeInTheDocument();

    fetchHighlightsMock.mockResolvedValueOnce({ status: "ready", items: [] });
    rerender(<PipelineHighlightsPage runId={`${runId.slice(0, -1)}b`} token="tok" onBackToChat={vi.fn()} />);
    expect(await screen.findByText(/No committed highlight candidates/i)).toBeInTheDocument();
  });

  it("does not fetch an invalid direct-link identifier", async () => {
    render(<PipelineHighlightsPage runId="pipeline_run_invalid" token="tok" onBackToChat={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("invalid pipeline run identifier");
    expect(fetchHighlightsMock).not.toHaveBeenCalled();
  });

  it("offers refresh after a gateway error", async () => {
    fetchHighlightsMock
      .mockRejectedValueOnce(new Error("gateway failed"))
      .mockResolvedValueOnce({ status: "ready", items: [] });
    render(<PipelineHighlightsPage runId={runId} token="tok" onBackToChat={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("could not be loaded");
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetchHighlightsMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/No committed highlight candidates/i)).toBeInTheDocument();
  });
});
