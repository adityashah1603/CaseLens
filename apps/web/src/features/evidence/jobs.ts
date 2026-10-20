import type { InfiniteData } from "@tanstack/react-query";
import type { EvidenceList, IngestJob } from "../../api/types.ts";

/** Applies one live job update to every cached evidence list page. */
export function applyJobUpdate(data: InfiniteData<EvidenceList> | undefined, job: IngestJob): InfiniteData<EvidenceList> | undefined {
  if (data === undefined) return data;
  return {
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      items: page.items.map((ev) => {
        if (ev.id !== job.evidence_id) return ev;
        const exists = ev.ingest_jobs.some((j) => j.id === job.id);
        return {
          ...ev,
          ingest_jobs: exists ? ev.ingest_jobs.map((j) => (j.id === job.id ? job : j)) : [...ev.ingest_jobs, job],
        };
      }),
    })),
  };
}
