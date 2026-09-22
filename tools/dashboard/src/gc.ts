// GoatCounter API client. Every stats endpoint the API exposes is pulled and
// surfaced — nothing the counter records is hidden from the operator.
// Calls go through the vite /gc proxy, which injects the token server-side.

export type DayStat = {
  day: string;
  hourly: number[];
  daily: number;
};

export type Total = {
  total: number;
  total_events: number;
  stats: DayStat[];
};

export type PageHit = {
  count: number;
  path: string;
  title: string;
  event: boolean;
  stats: DayStat[];
};

export type NamedStat = {
  id?: string;
  name: string;
  count: number;
  ref_scheme?: string | null;
};

export type Snapshot = {
  fetchedAt: Date;
  start: string;
  total: Total;
  pages: PageHit[];
  referrers: NamedStat[];
  locations: NamedStat[];
  languages: NamedStat[];
  browsers: NamedStat[];
  systems: NamedStat[];
  sizes: NamedStat[];
  campaigns: NamedStat[];
};

export class RateLimited extends Error {
  retryAfterSec: number;
  constructor(retryAfterSec: number) {
    super(`rate-limited; retry in ${retryAfterSec}s`);
    this.retryAfterSec = retryAfterSec;
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`/gc/api/v0${path}`);
  if (res.status === 429) {
    // The API budget is 500 requests per window; retry-after says when the
    // window resets. Surface it so the operator knows the actual wait.
    throw new RateLimited(parseInt(res.headers.get("retry-after") ?? "60", 10));
  }
  if (!res.ok) {
    throw new Error(`GoatCounter API ${res.status} on ${path}`);
  }
  return res.json() as Promise<T>;
}

const named = (key: "stats") => async (path: string) =>
  (await get<Record<string, NamedStat[]>>(path))[key] ?? [];

// The API allows roughly four requests per second; nine dimensions fired in
// parallel trip its 429. Fetch sequentially with a small gap — a poll cycle
// taking ~3 s is invisible at a 30 s cadence, and never rate-limits.
const GAP_MS = 350;

export async function fetchSnapshot(start: string): Promise<Snapshot> {
  const q = `?start=${start}`;
  const list = named("stats");
  const total = await get<Total>(`/stats/total${q}`);
  await sleep(GAP_MS);
  const hits = await get<{ hits: PageHit[] }>(`/stats/hits${q}&limit=100`);
  const dims: NamedStat[][] = [];
  for (const ep of ["toprefs", "locations", "languages", "browsers", "systems", "sizes", "campaigns"]) {
    await sleep(GAP_MS);
    dims.push(await list(`/stats/${ep}${q}`));
  }
  const [referrers, locations, languages, browsers, systems, sizes, campaigns] = dims;
  return {
    fetchedAt: new Date(),
    start,
    total,
    pages: hits.hits ?? [],
    referrers,
    locations,
    languages,
    browsers,
    systems,
    sizes,
    campaigns,
  };
}

// ---- derived readings ------------------------------------------------------

const iso = (d: Date) => d.toISOString().slice(0, 10);

export function todayCount(t: Total): number {
  const today = iso(new Date());
  return t.stats.find((s) => s.day === today)?.daily ?? 0;
}

export function lastNDays(t: Total, n: number): DayStat[] {
  const today = iso(new Date());
  return t.stats.filter((s) => s.day <= today).slice(-n);
}

export function weekCount(t: Total): number {
  return lastNDays(t, 7).reduce((a, s) => a + s.daily, 0);
}

export function todayHourly(t: Total): number[] {
  const today = iso(new Date());
  return t.stats.find((s) => s.day === today)?.hourly ?? new Array(24).fill(0);
}

export function busiestHour(t: Total): { hour: number; count: number } {
  const h = todayHourly(t);
  const hour = h.reduce((best, v, i) => (v > h[best] ? i : best), 0);
  return { hour, count: h[hour] };
}

// Device-size ids arrive with empty names; give them reader-facing labels.
const SIZE_LABELS: Record<string, string> = {
  phone: "Phone",
  largephone: "Large phone",
  tablet: "Tablet",
  desktop: "Desktop",
  desktophd: "Large desktop",
  unknown: "Unknown",
};

export function sizeLabel(s: NamedStat): string {
  return s.name || SIZE_LABELS[s.id ?? ""] || s.id || "Unknown";
}

export function refLabel(s: NamedStat): string {
  return s.name === "" ? "(direct or none)" : s.name;
}

// Injected by vite.config.ts from data/released/*/issue.json.
declare const ISSUE_NUMBERS: Record<string, string>;

// GoatCounter records the path exactly as the browser sent it, so the same
// page can arrive under several spellings. Fold them before display.
function normalizePath(path: string): string {
  let p = path.split("?")[0];
  if (p.endsWith("/index.html")) p = p.slice(0, -"index.html".length);
  if (p.length > 1 && p.endsWith("/")) p = p.slice(0, -1);
  return p === "" ? "/" : p;
}

// Merge hits that normalise to the same page: counts sum, the first row's
// title and day-stats stand for the group.
export function mergePages(pages: PageHit[]): PageHit[] {
  const byPath = new Map<string, PageHit>();
  for (const p of pages) {
    const key = normalizePath(p.path);
    const existing = byPath.get(key);
    if (existing) {
      existing.count += p.count;
    } else {
      byPath.set(key, { ...p, path: key });
    }
  }
  return [...byPath.values()].sort((a, b) => b.count - a.count);
}

export function pageLabel(p: PageHit): { name: string; detail: string } {
  if (p.path === "/") return { name: "The landing page", detail: "/" };
  const issue = ISSUE_NUMBERS[p.path];
  if (issue) {
    const date = p.path.slice(10, 20);
    return { name: `${issue} — ${formatDay(date)}`, detail: p.path };
  }
  if (p.path.includes("how-its-made")) return { name: "How it's made", detail: p.path };
  if (p.path === "/about.html") return { name: "About (redirect)", detail: p.path };
  return { name: p.title || p.path, detail: p.path };
}

export function formatDay(isoDay: string): string {
  return new Date(`${isoDay}T00:00:00`).toLocaleDateString("en-AU", {
    day: "numeric",
    month: "short",
  });
}
