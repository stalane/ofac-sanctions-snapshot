/* OFAC Sanctions Snapshot API — Cloudflare Worker + D1 serving cache.
 * Read-only: D1 is never written at request time. Data is seeded weekly by
 * .github/workflows/reseed.yml, so /api/refresh is a no-op and /api/progress
 * always reports idle — the frontend tolerates both.
 *
 * Every response is precomputed at seed time by db.precompute() and stored as
 * five small tables (meta, country_totals, country_detail, program_counts,
 * list_counts). Each handler below is a single primary-key lookup. This
 * replaced running aggregations per request, which full-scanned entities,
 * programs and lists on every /api/country/<name> call: 92% of all D1 rows read,
 * and a 35s timeout that 500'd Russia outright.
 *
 * Edge caching: snapshot data changes weekly at most. GET /api/* responses are
 * cached at the edge so steady state costs ~zero D1 reads. TTLs are short for
 * /api/meta (readiness signal) and long for the rest.
 *
 * The Worker code and the serving schema ship in one commit — a new index.js
 * against an old-schema database 500s every endpoint. */

function cacheTtl(path) {
  if (path === "/api/meta") return 60;
  if (path === "/api/countries" || path === "/api/programs" || path === "/api/lists") return 21600;
  if (path.startsWith("/api/country/")) return 21600;
  return 0;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function handleMeta(db) {
  const { results } = await db.prepare(
    "SELECT key, value FROM meta WHERE key IN " +
    "('data_as_of', 'entities', 'countries', 'programs', 'lists')"
  ).all();
  const m = {};
  for (const row of results) m[row.key] = row.value;
  // meta.value is TEXT; the frontend expects numbers. An absent entities row
  // is the not-ready signal (fresh or partially seeded database).
  const entities = Number(m.entities);
  if (!Number.isFinite(entities) || entities === 0) {
    return json({
      ready: false, refreshing: false, data_as_of: null,
      entities: 0, countries: 0, programs: 0, lists: 0,
    });
  }
  return json({
    ready: true, refreshing: false, data_as_of: m.data_as_of,
    entities,
    countries: Number(m.countries),
    programs: Number(m.programs),
    lists: Number(m.lists),
  });
}

async function handleCountries(db) {
  const { results } = await db.prepare(`
    SELECT country, total, individuals, organizations, vessels
    FROM country_totals
    ORDER BY total DESC, country ASC
  `).all();
  return json({ data: results });
}

async function handleCountry(db, name) {
  let country;
  try {
    country = decodeURIComponent(name);
  } catch {
    return json({ error: "bad country encoding" }, 400);
  }
  const row = await db.prepare(
    "SELECT payload FROM country_detail WHERE country = ?"
  ).bind(country).first();
  if (!row) {
    return json({ error: "country not found" }, 404);
  }
  return json(JSON.parse(row.payload));
}

async function handlePrograms(db) {
  const { results } = await db.prepare(`
    SELECT program, n AS count
    FROM program_counts
    ORDER BY n DESC, program ASC
  `).all();
  return json({ data: results });
}

async function handleLists(db) {
  const { results } = await db.prepare(`
    SELECT list_name, n AS count
    FROM list_counts
    ORDER BY n DESC, list_name ASC
  `).all();
  return json({ data: results });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const db = env.DB;

    // Edge-cache GET /api/* (snapshot data changes weekly; D1 free reads
    // are capped). Only 200s are stored; errors always hit D1 fresh so a
    // capped backend surfaces instead of serving stale errors.
    if (request.method === "GET") {
      const ttl = cacheTtl(path);
      if (ttl > 0) {
        const cache = caches.default;
        const hit = await cache.match(request);
        if (hit) return hit;
        const res = await routeApi(db, path, request);
        if (res && res.status === 200) {
          ctx.waitUntil(cache.put(request, res.clone()));
        }
        return res;
      }
    }
    const routed = await routeApi(db, path, request);
    if (routed) return routed;

    // Parity redirects from the Flask app (assets live under /static/).
    if (path === "/favicon.ico") return Response.redirect("/static/favicon.ico", 302);
    if (path === "/robots.txt") return Response.redirect("/static/robots.txt", 302);
    if (path === "/manifest.json") return Response.redirect("/static/manifest.json", 302);

    // Anything else (/, /static/*) falls through to Static Assets.
    // Guarded: without an assets binding env.ASSETS is undefined and every
    // missing-asset path would throw (HTTP 500) instead of 404.
    if (env.ASSETS) return env.ASSETS.fetch(request);
    return new Response("Not found", {
      status: 404,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};

async function routeApi(db, path, request) {
  const ttl = cacheTtl(path);
  if (path === "/api/meta") return withTtl(handleMeta(db), ttl);
  if (path === "/api/progress") {
    return json({ phase: "idle", bytes: 0, total_bytes: null, entities: 0 });
  }
  if (path === "/api/countries") return withTtl(handleCountries(db), ttl);
  if (path.startsWith("/api/country/")) {
    return withTtl(handleCountry(db, path.slice("/api/country/".length)), ttl);
  }
  if (path === "/api/programs") return withTtl(handlePrograms(db), ttl);
  if (path === "/api/lists") return withTtl(handleLists(db), ttl);
  if (path === "/api/refresh" && request.method === "POST") {
    return json({ refreshing: false, note: "data refreshes via scheduled pipeline" });
  }
  return null;
}

async function withTtl(promise, ttl) {
  const res = await promise;
  if (ttl > 0 && res.status === 200) {
    const headers = new Headers(res.headers);
    headers.set("cache-control", `public, s-maxage=${ttl}`);
    return new Response(res.body, { status: res.status, headers });
  }
  return res;
}
