export interface LayoutNode {
  id: string;
  x: number;
  y: number;
}

/**
 * A small deterministic force-directed layout (repulsion between all nodes, springs on edges,
 * gentle pull to the centre). Starts from a circle, so the same graph always lays out the same.
 */
export function forceLayout(
  ids: readonly string[],
  edges: readonly { source: string; target: string }[],
  width: number,
  height: number,
  iterations = 250,
): Map<string, LayoutNode> {
  const n = ids.length;
  const cx = width / 2;
  const cy = height / 2;
  const nodes: LayoutNode[] = ids.map((id, i) => {
    const a = (2 * Math.PI * i) / Math.max(1, n);
    const r = Math.min(width, height) * 0.35;
    return { id, x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
  });
  const index = new Map(nodes.map((nd, i) => [nd.id, i]));
  const springs = edges
    .map((e) => [index.get(e.source), index.get(e.target)] as const)
    .filter((p): p is readonly [number, number] => p[0] !== undefined && p[1] !== undefined && p[0] !== p[1]);
  const k = Math.sqrt((width * height) / Math.max(1, n)) * 0.6;
  const dx = new Float64Array(n);
  const dy = new Float64Array(n);

  for (let it = 0; it < iterations; it++) {
    const temp = (1 - it / iterations) * k * 0.5 + 1;
    dx.fill(0);
    dy.fill(0);
    for (let i = 0; i < n; i++) {
      const a = nodes[i];
      if (a === undefined) continue;
      for (let j = i + 1; j < n; j++) {
        const b = nodes[j];
        if (b === undefined) continue;
        let vx = a.x - b.x;
        let vy = a.y - b.y;
        let d2 = vx * vx + vy * vy;
        if (d2 < 0.01) {
          vx = 0.1 * (i - j);
          vy = 0.1;
          d2 = vx * vx + vy * vy;
        }
        const f = (k * k) / d2;
        dx[i] = (dx[i] ?? 0) + vx * f;
        dy[i] = (dy[i] ?? 0) + vy * f;
        dx[j] = (dx[j] ?? 0) - vx * f;
        dy[j] = (dy[j] ?? 0) - vy * f;
      }
    }
    for (const [i, j] of springs) {
      const a = nodes[i];
      const b = nodes[j];
      if (a === undefined || b === undefined) continue;
      const vx = a.x - b.x;
      const vy = a.y - b.y;
      const d = Math.sqrt(vx * vx + vy * vy) || 0.01;
      const f = d / k;
      dx[i] = (dx[i] ?? 0) - vx * f;
      dy[i] = (dy[i] ?? 0) - vy * f;
      dx[j] = (dx[j] ?? 0) + vx * f;
      dy[j] = (dy[j] ?? 0) + vy * f;
    }
    for (let i = 0; i < n; i++) {
      const nd = nodes[i];
      if (nd === undefined) continue;
      const mx = (dx[i] ?? 0) + (cx - nd.x) * 0.02;
      const my = (dy[i] ?? 0) + (cy - nd.y) * 0.02;
      const len = Math.sqrt(mx * mx + my * my) || 1;
      const step = Math.min(len, temp);
      nd.x = Math.min(width - 20, Math.max(20, nd.x + (mx / len) * step));
      nd.y = Math.min(height - 20, Math.max(20, nd.y + (my / len) * step));
    }
  }
  return new Map(nodes.map((nd) => [nd.id, nd]));
}
