import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import {
  fetchPipelineHighlights,
  isPipelineRunId,
  type PipelineHighlightItem,
  type PipelineHighlightsPayload,
} from "@/lib/api";
import { Button } from "@/components/ui/button";

type HighlightsState =
  | { status: "loading" }
  | { status: "invalid" }
  | { status: "error" }
  | { status: "not_ready" }
  | { status: "ready"; items: PipelineHighlightItem[] };

function confidence(value: string): string {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${Math.round(numeric * 100)}%` : value;
}

function DetailList({ label, values }: { label: string; values: string[] }) {
  if (values.length === 0) return null;
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-1 flex flex-wrap gap-1.5">
        {values.map((value) => (
          <span className="rounded-full bg-muted px-2 py-0.5 text-xs" key={value}>{value}</span>
        ))}
      </dd>
    </div>
  );
}

function HighlightCard({ item }: { item: PipelineHighlightItem }) {
  const window = item.semantic_window;
  return (
    <article className="rounded-lg border border-border bg-card p-5 shadow-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-semibold">Episode {item.episode_index + 1}</h2>
        <span className="font-mono text-xs text-muted-foreground">{item.candidate_id}</span>
      </div>
      <p className="mt-3 text-sm leading-6">{item.reason}</p>
      <dl className="mt-4 grid gap-4 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Anchor</dt>
          <dd className="mt-1 leading-6">{item.anchor_summary}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Payoff</dt>
          <dd className="mt-1 leading-6">{item.payoff_summary}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Support confidence</dt>
          <dd className="mt-1">{confidence(item.support_confidence)}</dd>
        </div>
        <DetailList label="Tags" values={item.tags} />
        <DetailList label="Narrative functions" values={item.narrative_functions} />
        <DetailList label="Editing modes" values={item.editing_modes} />
      </dl>
      {item.dialogue_excerpt ? (
        <blockquote className="mt-4 border-l-2 border-primary/40 pl-3 text-sm italic leading-6 text-muted-foreground">
          {item.dialogue_excerpt}
        </blockquote>
      ) : null}
      <section className="mt-5 rounded-md bg-muted/55 p-3" aria-label="Semantic window (coarse)">
        <h3 className="text-sm font-medium">Semantic window (coarse)</h3>
        <p className="mt-1 text-sm text-muted-foreground">
          {window.start_tick}–{window.end_tick} ticks · source time base {window.source_time_base.numerator}/{window.source_time_base.denominator}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Mapping uncertainty: {window.mapping_error_bound_source_ticks} source ticks. Provider uncertainty: {window.provider_uncertainty_proxy_ticks} ticks at {window.provider_uncertainty_proxy_time_base.numerator}/{window.provider_uncertainty_proxy_time_base.denominator}.
        </p>
      </section>
      {item.measurements.length > 0 ? (
        <dl className="mt-4 grid gap-2 text-sm sm:grid-cols-2">
          {item.measurements.map((measurement) => (
            <div className="rounded-md border border-border/70 px-3 py-2" key={measurement.kind}>
              <dt className="text-xs font-medium text-muted-foreground">{measurement.kind}</dt>
              <dd className="mt-1">{measurement.value} <span className="text-muted-foreground">({confidence(measurement.confidence)})</span></dd>
            </div>
          ))}
        </dl>
      ) : null}
    </article>
  );
}

export function PipelineHighlightsPage({
  runId,
  token,
  onBackToChat,
}: {
  runId: string;
  token: string;
  onBackToChat: () => void;
}) {
  const [state, setState] = useState<HighlightsState>({ status: "loading" });

  const load = useCallback(async () => {
    if (!isPipelineRunId(runId)) {
      setState({ status: "invalid" });
      return;
    }
    setState({ status: "loading" });
    try {
      const payload: PipelineHighlightsPayload = await fetchPipelineHighlights(token, runId);
      setState(payload.status === "not_ready"
        ? { status: "not_ready" }
        : { status: "ready", items: payload.items });
    } catch {
      setState({ status: "error" });
    }
  }, [runId, token]);

  useEffect(() => {
    void load();
  }, [load]);

  const body = (() => {
    if (state.status === "loading") {
      return <p aria-live="polite" className="text-sm text-muted-foreground">Loading highlight candidates…</p>;
    }
    if (state.status === "invalid") {
      return <p role="alert" className="text-sm text-destructive">This highlight link has an invalid pipeline run identifier.</p>;
    }
    if (state.status === "error") {
      return <p role="alert" className="text-sm text-destructive">Highlight candidates could not be loaded. Refresh to try again.</p>;
    }
    if (state.status === "not_ready") {
      return <p className="text-sm text-muted-foreground">Highlight candidates are not ready yet. This page will only show committed semantic evidence.</p>;
    }
    if (state.items.length === 0) {
      return <p className="text-sm text-muted-foreground">No committed highlight candidates are available for this run.</p>;
    }
    return <div className="grid gap-4">{state.items.map((item) => <HighlightCard item={item} key={item.candidate_id} />)}</div>;
  })();

  return (
    <section className="h-full overflow-y-auto px-5 py-8 sm:px-8 lg:px-12" aria-labelledby="pipeline-highlights-title">
      <div className="mx-auto max-w-4xl">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-sm text-muted-foreground">Read-only pipeline evidence</p>
            <h1 className="mt-1 text-2xl font-semibold" id="pipeline-highlights-title">Highlight candidates</h1>
            <p className="mt-2 text-sm text-muted-foreground">Semantic windows are coarse observations, not media edit instructions.</p>
          </div>
          <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={onBackToChat}>Open a chat</Button>
            <Button type="button" variant="outline" onClick={() => void load()} disabled={state.status === "loading"}>
              <RefreshCw aria-hidden="true" className="mr-2 h-4 w-4" /> Refresh
            </Button>
          </div>
        </div>
        <div className="mt-8">{body}</div>
      </div>
    </section>
  );
}
