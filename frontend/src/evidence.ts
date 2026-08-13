import { getEvidence, type DecisionAudit, type EvidenceChain } from "./api";

export class EvidenceView {
  constructor(private readonly host: HTMLElement) {}

  render(rows: DecisionAudit[]): void {
    this.host.replaceChildren();
    const recent = rows.slice(-3).reverse();
    if (!recent.length) {
      const empty = document.createElement("span");
      empty.className = "evidence-empty";
      empty.textContent = "No recommendations yet";
      this.host.append(empty);
      return;
    }
    for (const row of recent) this.host.append(this.summary(row));
  }

  private summary(row: DecisionAudit): HTMLDetailsElement {
    const details = document.createElement("details");
    details.className = "evidence-item";
    const summary = document.createElement("summary");
    const outcome = row.risk_decision?.outcome ?? "not evaluated";
    summary.textContent = `${row.proposal.side.toUpperCase()} ${row.proposal.quantity} ${row.proposal.symbol} · ${outcome}`;
    const body = document.createElement("div");
    body.className = "evidence-body";
    body.textContent = "Open to load evidence";
    details.append(summary, body);
    details.addEventListener("toggle", () => {
      if (details.open && body.dataset.loaded !== "true") {
        body.textContent = "Loading evidence…";
        getEvidence(row.proposal.proposal_id)
          .then((chain) => {
            body.dataset.loaded = "true";
            body.replaceChildren(this.chainContent(chain));
          })
          .catch(() => { body.textContent = "Evidence unavailable"; });
      }
    });
    return details;
  }

  private chainContent(chain: EvidenceChain): DocumentFragment {
    const fragment = document.createDocumentFragment();
    fragment.append(this.line(chain.proposal.rationale, "Rationale"));
    fragment.append(this.line(
      `${chain.market_observation.mode} · ${chain.market_observation.source} · ${formatTime(chain.market_observation.market_timestamp)}`,
      "Market observation",
    ));
    for (const claim of chain.research.claims) {
      const relevant = chain.proposal.evidence_claim_ids.includes(claim.claim_id);
      if (!relevant) continue;
      fragment.append(this.line(`${claim.claim} (${claim.stance}, ${claim.confidence})`, "Claim"));
      for (const sourceId of claim.source_ids) {
        const source = chain.research.sources.find((item) => item.source_id === sourceId);
        if (!source) continue;
        const link = document.createElement("a");
        link.href = source.canonical_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = `${source.publisher}: ${source.title} · ${formatTime(source.published_at)}`;
        link.title = source.supporting_excerpt;
        fragment.append(link);
      }
    }
    const failed = chain.risk_decision?.rules.filter((rule) => !rule.passed) ?? [];
    fragment.append(this.line(
      chain.risk_decision ? `${chain.risk_decision.outcome}${failed.length ? ` · ${failed.map((r) => r.reason).join("; ")}` : ""}` : "not evaluated",
      "Risk",
    ));
    fragment.append(this.line(chain.execution?.status ?? "not executed", "Paper execution"));
    fragment.append(this.line(
      `${chain.prompt_versions.researcher} · ${chain.prompt_versions.trader}`,
      "Prompt versions",
    ));
    return fragment;
  }

  private line(value: string, label: string): HTMLParagraphElement {
    const row = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = `${label}: `;
    row.append(strong, value);
    return row;
  }
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString();
}
