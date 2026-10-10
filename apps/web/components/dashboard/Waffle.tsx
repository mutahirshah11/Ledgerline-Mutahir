// "Line grid" from the redesign handoff (README "Dashboard — Runs", hero waffle): one square per
// delivery line, 25 columns, so a reviewer sees at a glance how much of the month is settled.
// Built from real per-line outcomes (GET /runs/{id}/groups), not approximated from aggregate
// counts, so the pattern of amber squares genuinely corresponds to the flagged lines.

export type WaffleCell = "matched" | "needs" | "resolved";

const CELL_CLASS: Record<WaffleCell, string> = {
  matched: "bg-success",
  needs: "bg-warning",
  resolved: "bg-white/70",
};

export type WaffleProps = {
  cells: WaffleCell[];
  columns?: number;
  /** On a dark (hero) background vs. a light card background. */
  tone?: "dark" | "light";
};

export function Waffle({ cells, columns = 25, tone = "dark" }: WaffleProps) {
  return (
    <div
      role="img"
      aria-label={`${cells.length} delivery lines: ${cells.filter((c) => c === "matched").length} matched, ${
        cells.filter((c) => c === "needs").length
      } need a decision.`}
      className="grid gap-[3px]"
      style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
    >
      {cells.map((cell, i) => (
        <div
          key={i}
          aria-hidden="true"
          className={`aspect-square rounded-[2.5px] ${CELL_CLASS[cell]} ${
            tone === "dark" && cell === "resolved" ? "bg-white/40" : ""
          }`}
        />
      ))}
    </div>
  );
}
