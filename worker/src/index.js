/* OFAC Sanctions Snapshot API — Cloudflare Worker + D1 port of app.py/db.py.
 * Read-only: data is seeded from the SQLite snapshot (see seed notes).
 * Refresh is handled by an external pipeline, so /api/refresh is a no-op
 * and /api/progress always reports idle — the frontend tolerates both. */

/* OFAC Sanctions Snapshot API — Cloudflare Worker + D1 port of app.py/db.py.
 * Read-only: data is seeded from the SQLite snapshot (see seed notes).
 * Refresh is handled by an external pipeline, so /api/refresh is a no-op
 * and /api/progress always reports idle — the frontend tolerates both.
 *
 * Edge caching: snapshot data changes weekly at most, but the D1 free
 * tier caps reads at 5M rows/day (full-table aggregations scan tens of
 * thousands of rows per call). GET /api/* responses are cached at the
 * edge so steady state costs ~zero D1 reads. TTLs are short for /api/meta
 * (readiness signal) and long for the rest. */

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
  const [asOf, entities, countries, programs, lists] = await Promise.all([
    db.prepare("SELECT value FROM meta WHERE key = 'data_as_of'").first("value"),
    db.prepare("SELECT COUNT(*) AS n FROM entities").first("n"),
    db.prepare("SELECT COUNT(DISTINCT country) AS n FROM countries").first("n"),
    db.prepare("SELECT COUNT(DISTINCT program) AS n FROM programs").first("n"),
    db.prepare("SELECT COUNT(DISTINCT list_name) AS n FROM lists").first("n"),
  ]);
  if (!entities) {
    return json({
      ready: false, refreshing: false, data_as_of: null,
      entities: 0, countries: 0, programs: 0, lists: 0,
    });
  }
  return json({
    ready: true, refreshing: false, data_as_of: asOf,
    entities, countries, programs, lists,
  });
}

async function handleCountries(db) {
  const { results } = await db.prepare(`
    SELECT d.country,
           COUNT(DISTINCT d.entity_id) AS total,
           SUM(CASE WHEN e.entity_type = 'Individual' THEN 1 ELSE 0 END) AS individuals,
           SUM(CASE WHEN e.entity_type = 'Entity' THEN 1 ELSE 0 END) AS organizations,
           SUM(CASE WHEN e.entity_type = 'Vessel' THEN 1 ELSE 0 END) AS vessels
    FROM (SELECT DISTINCT entity_id, country FROM countries) d
    JOIN entities e ON e.id = d.entity_id
    GROUP BY d.country
    ORDER BY total DESC
  `).all();
  return json({ data: results });
}

const DEDUP = "(SELECT DISTINCT entity_id FROM countries WHERE country = ?)";

async function handleCountry(db, name) {
  const country = decodeURIComponent(name);
  const [types, programs, lists, entities] = await Promise.all([
    db.prepare(`
      SELECT e.entity_type, COUNT(*) AS count
      FROM entities e
      JOIN ${DEDUP} d ON d.entity_id = e.id
      GROUP BY e.entity_type
      ORDER BY count DESC`).bind(country).all(),
    db.prepare(`
      SELECT p.program, COUNT(DISTINCT p.entity_id) AS count
      FROM programs p
      JOIN ${DEDUP} d ON d.entity_id = p.entity_id
      GROUP BY p.program
      ORDER BY count DESC
      LIMIT 12`).bind(country).all(),
    db.prepare(`
      SELECT l.list_name, COUNT(DISTINCT l.entity_id) AS count
      FROM lists l
      JOIN ${DEDUP} d ON d.entity_id = l.entity_id
      GROUP BY l.list_name
      ORDER BY count DESC
      LIMIT 8`).bind(country).all(),
    db.prepare(`
      SELECT e.id, e.name, e.entity_type,
             COALESCE(GROUP_CONCAT(DISTINCT p.program), '') AS programs,
             COALESCE(GROUP_CONCAT(DISTINCT l.list_name), '') AS lists
      FROM entities e
      LEFT JOIN programs p ON p.entity_id = e.id
      LEFT JOIN lists l ON l.entity_id = e.id
      JOIN ${DEDUP} d ON d.entity_id = e.id
      GROUP BY e.id
      ORDER BY e.name
      LIMIT 50`).bind(country).all(),
  ]);
  if (!entities.results.length && !types.results.length) {
    return json({ error: "country not found" }, 404);
  }
  return json({
    types: types.results, programs: programs.results,
    lists: lists.results, entities: entities.results,
  });
}

async function handlePrograms(db) {
  const { results } = await db.prepare(`
    SELECT program, COUNT(DISTINCT entity_id) AS count
    FROM programs
    GROUP BY program
    ORDER BY count DESC
    LIMIT 20`).all();
  return json({ data: results });
}

async function handleLists(db) {
  const { results } = await db.prepare(`
    SELECT list_name, COUNT(DISTINCT entity_id) AS count
    FROM lists
    GROUP BY list_name
    ORDER BY count DESC`).all();
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
    return env.ASSETS.fetch(request);
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
