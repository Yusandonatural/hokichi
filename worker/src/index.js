/**
 * hokichi 社内配信用 Worker。
 * 静的ファイル（web/）を配信し、HTTP Basic 認証で社内限定にする。
 *   環境変数: HOKICHI_USER, HOKICHI_PASSWORD（wrangler secret put で設定）
 *   未設定なら 503 を返して何も配信しない（うっかり公開を防ぐ）。
 * Cloudflare Access を使う場合は AUTH_MODE=access にすると Basic 認証をスキップする。
 */
export default {
  async fetch(request, env) {
    if (env.AUTH_MODE !== "access") {
      if (!env.HOKICHI_USER || !env.HOKICHI_PASSWORD) {
        return new Response("認証情報が未設定です。wrangler secret put HOKICHI_USER / HOKICHI_PASSWORD を実行してください。",
          { status: 503, headers: { "content-type": "text/plain; charset=utf-8" } });
      }
      const auth = request.headers.get("Authorization") || "";
      const ok = auth.startsWith("Basic ") && safeEqual(atob(auth.slice(6)), `${env.HOKICHI_USER}:${env.HOKICHI_PASSWORD}`);
      if (!ok) {
        return new Response("認証が必要です", {
          status: 401,
          headers: { "WWW-Authenticate": 'Basic realm="hokichi", charset="UTF-8"', "content-type": "text/plain; charset=utf-8" },
        });
      }
    }
    const res = await env.ASSETS.fetch(request);
    const h = new Headers(res.headers);
    h.set("X-Robots-Tag", "noindex, nofollow");
    h.set("Cache-Control", request.url.includes("/data/") ? "private, max-age=300" : "private, max-age=3600");
    h.set("Referrer-Policy", "no-referrer");
    return new Response(res.body, { status: res.status, headers: h });
  },
};

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
