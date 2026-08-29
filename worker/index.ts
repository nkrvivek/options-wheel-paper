/**
 * Cloudflare Worker for the paper-wheel bot.
 *
 * This Worker is now the ONLY scheduler and the only thing that can start a
 * trading run. It replaces `options-wheel-dispatch`, which fired a GitHub
 * workflow_dispatch at the same minute and let a GH runner do the trading.
 * GitHub keeps exactly one job: building the container image (see
 * .github/workflows/deploy-wheel.yml).
 *
 * Routes:
 *   GET  /                 public  — worker liveness (no container spin-up)
 *   GET  /health           public  — worker + container liveness probe
 *   GET  /status           token   — container /status (last run, R2 state)
 *   POST /run-daily        token   — manual run, same path the cron uses
 *   POST /container-restart token  — recycle the container after a deploy
 *
 * Token routes accept WORKER_AUTH_TOKEN via the X-Worker-Token header or a
 * ?token= query param. The Worker then talks to the container with the
 * separate CONTAINER_AUTH_TOKEN, so the public-facing secret is never the one
 * the container trusts.
 */
import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  WHEEL_CONTAINER: DurableObjectNamespace<WheelContainer>;
  WHEEL_STATE: R2Bucket;
  GIT_COMMIT?: string;
  IS_PAPER?: string;
  CONTAINER_R2_BUCKET?: string;
  WORKER_AUTH_TOKEN?: string;
  CONTAINER_AUTH_TOKEN?: string;
  ALPACA_API_KEY?: string;
  ALPACA_SECRET_KEY?: string;
  TR_WORKER_URL?: string;
  TR_WORKER_TOKEN?: string;
  RESEND_API_KEY?: string;
  RESEND_FROM?: string;
  RESEND_TO?: string;
  R2_ACCOUNT_ID?: string;
  R2_ENDPOINT?: string;
  R2_ACCESS_KEY_ID?: string;
  R2_SECRET_ACCESS_KEY?: string;
}

/** Secrets forwarded verbatim into the container env. */
const FORWARDED_SECRETS = [
  "CONTAINER_AUTH_TOKEN",
  "ALPACA_API_KEY",
  "ALPACA_SECRET_KEY",
  "TR_WORKER_URL",
  "TR_WORKER_TOKEN",
  "RESEND_API_KEY",
  "RESEND_FROM",
  "RESEND_TO",
  "R2_ACCOUNT_ID",
  "R2_ENDPOINT",
  "R2_ACCESS_KEY_ID",
  "R2_SECRET_ACCESS_KEY",
] as const;

/** Plain vars forwarded into the container env. */
const FORWARDED_VARS = ["GIT_COMMIT", "IS_PAPER"] as const;

function buildContainerEnv(env: Env): Record<string, string> {
  const out: Record<string, string> = {};
  const raw = env as unknown as Record<string, string | undefined>;
  for (const k of [...FORWARDED_VARS, ...FORWARDED_SECRETS]) {
    const v = raw[k];
    if (v) out[k] = v;
  }
  // CONTAINER_R2_BUCKET is the wrangler-side name; the Python side reads R2_BUCKET.
  const bucket = raw["CONTAINER_R2_BUCKET"];
  if (bucket) out["R2_BUCKET"] = bucket;
  // Boot assertion in server.py: refuse to start if R2 did not resolve, so a
  // credential typo cannot silently write state to an ephemeral container disk.
  out["WHEEL_REQUIRE_R2"] = "1";
  // Paper-only book. The bot reads IS_PAPER to pick the Alpaca endpoint; a
  // missing/!=true value would point a paper key set at the live host.
  if (out["IS_PAPER"] !== "true") {
    throw new Error(
      `[buildContainerEnv] SAFETY: IS_PAPER resolved to ${JSON.stringify(out["IS_PAPER"])}, expected "true". ` +
        "This book is paper-only (Alpaca PA3OF0SFKF40); set IS_PAPER = \"true\" in wrangler.toml [vars].",
    );
  }
  return out;
}

export class WheelContainer extends Container<Env> {
  defaultPort = 8080;
  // The bot runs once a day. Idling the container between runs is the whole
  // point of the sleep timer; the next cron cold-starts it in seconds.
  sleepAfter = "15m";
  private envHash = "";

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.envVars = buildContainerEnv(env);
    this.envHash = JSON.stringify(this.envVars);
    // Restart ONLY when env actually changed across a DO eviction — a blanket
    // stop() in the ctor kills a run mid-flight, since the runtime reconstructs
    // DOs routinely (autopilot-experiment learned this the hard way, 2026-07-09).
    this.ctx.blockConcurrencyWhile(async () => {
      const started = await this.ctx.storage.get<string>("container_env_hash");
      if (started !== undefined && started !== this.envHash) {
        try {
          await this.container.stop();
        } catch {
          /* not running yet */
        }
      }
      await this.ctx.storage.put("container_env_hash", this.envHash);
    });
  }

  override async fetch(request: Request): Promise<Response> {
    const next = buildContainerEnv((this as unknown as { env: Env }).env);
    const nextHash = JSON.stringify(next);
    if (nextHash !== this.envHash) {
      this.envVars = next;
      this.envHash = nextHash;
      try {
        await this.container.stop();
      } catch {
        /* not running */
      }
      await this.ctx.storage.put("container_env_hash", nextHash);
    }
    const url = new URL(request.url);
    if (url.pathname === "/__restart" && request.method === "POST") {
      try {
        await this.container.stop();
      } catch {
        /* not running */
      }
      this.envVars = next;
      this.envHash = nextHash;
      await this.ctx.storage.put("container_env_hash", nextHash);
      return Response.json({ ok: true, restarted: true });
    }
    return super.fetch(request);
  }
}

function jsonResp(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj, null, 1), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function authed(request: Request, env: Env): boolean {
  const expected = env.WORKER_AUTH_TOKEN;
  if (!expected) return false;
  const url = new URL(request.url);
  const provided = request.headers.get("X-Worker-Token") ?? url.searchParams.get("token") ?? "";
  if (provided.length !== expected.length) return false;
  // Constant-time-ish compare; Workers has no timingSafeEqual.
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= provided.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

async function callContainer(
  env: Env,
  method: "GET" | "POST",
  path: string,
  query: Record<string, string> = {},
): Promise<Response> {
  const stub = getContainer(env.WHEEL_CONTAINER, "singleton");
  const qs = new URLSearchParams(query).toString();
  const url = `http://container${path}${qs ? "?" + qs : ""}`;
  return stub.fetch(url, {
    method,
    headers: { "X-Wheel-Token": env.CONTAINER_AUTH_TOKEN ?? "" },
  });
}

async function proxyContainer(
  env: Env,
  method: "GET" | "POST",
  path: string,
  query: Record<string, string> = {},
): Promise<Response> {
  try {
    const resp = await callContainer(env, method, path, query);
    return new Response(await resp.text(), {
      status: resp.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (e) {
    return jsonResp({ error: "container unreachable", detail: String(e) }, 502);
  }
}

/**
 * The one scheduled trading path.
 *
 * Cloudflare cron delivery is at-least-once, so a duplicate delivery of the
 * same (cron, scheduledTime) pair is claimed in R2 before anything dispatches.
 * The container's own /run-daily busy-lock is the second line of defence; this
 * one exists because the claim completes in milliseconds while a redelivery
 * gap is measured in seconds. Fail-open: an R2 error must never suppress a
 * legitimate fire — a duplicate run is caught downstream, a missed run is not.
 */
async function runDaily(env: Env, controller: ScheduledController): Promise<void> {
  const startedAt = new Date().toISOString();
  const dayKey = startedAt.slice(0, 10);
  const cronSlug = controller.cron.replace(/[^0-9A-Za-z]+/g, "_");
  const claimKey = `worker/cron_claim/${dayKey}/${cronSlug}_${controller.scheduledTime}.json`;
  try {
    const claimed = await env.WHEEL_STATE.head(claimKey);
    if (claimed) {
      console.warn(
        `[scheduled] duplicate delivery cron=${controller.cron} scheduledTime=${controller.scheduledTime} — skipping`,
      );
      return;
    }
    await env.WHEEL_STATE.put(
      claimKey,
      JSON.stringify({ startedAt, cron: controller.cron, scheduledTime: controller.scheduledTime }),
      { httpMetadata: { contentType: "application/json" } },
    );
  } catch (e) {
    console.warn(`[scheduled] claim failed (fail-open): ${String(e)}`);
  }

  await env.WHEEL_STATE.put(
    "worker/last_scheduled.json",
    JSON.stringify({ startedAt, cron: controller.cron, scheduledTime: controller.scheduledTime }),
    { httpMetadata: { contentType: "application/json" } },
  ).catch(() => {});

  try {
    const resp = await callContainer(env, "POST", "/run-daily", { trigger: "cron" });
    const body = await resp.text();
    console.log(`[scheduled:run-daily] container responded ${resp.status}: ${body.slice(0, 1500)}`);
    await env.WHEEL_STATE.put(
      `worker/last_run_dispatch.json`,
      JSON.stringify({ startedAt, cron: controller.cron, status: resp.status, body: body.slice(0, 4000) }),
      { httpMetadata: { contentType: "application/json" } },
    ).catch(() => {});
  } catch (e) {
    console.error(`[scheduled:run-daily] dispatch failed: ${String(e)}`);
    await env.WHEEL_STATE.put(
      `worker/cron_error/${dayKey}/${cronSlug}.json`,
      JSON.stringify({ startedAt, cron: controller.cron, error: String(e) }),
      { httpMetadata: { contentType: "application/json" } },
    ).catch(() => {});
  }
}

export default {
  async scheduled(controller: ScheduledController, env: Env, ctx: ExecutionContext) {
    console.log(`[scheduled] cron ${controller.cron} fired at ${new Date().toISOString()}`);
    ctx.waitUntil(runDaily(env, controller));
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // Public: no container spin-up, safe to poll.
    if (path === "/" || path === "/healthz") {
      return jsonResp({
        ok: true,
        service: "options-wheel-paper",
        git_commit: env.GIT_COMMIT ?? "unknown",
        schedule: "45 14 * * 2-6 UTC (10:45 ET Mon-Fri)",
        now: new Date().toISOString(),
      });
    }

    // Public: proves the container answers, without exposing run state.
    if (path === "/health") {
      try {
        const resp = await callContainer(env, "GET", "/healthz");
        const body = await resp.text();
        return jsonResp({
          ok: resp.ok,
          worker: "options-wheel-paper",
          git_commit: env.GIT_COMMIT ?? "unknown",
          container_status: resp.status,
          container: body.slice(0, 500),
        });
      } catch (e) {
        return jsonResp({ ok: false, error: "container unreachable", detail: String(e) }, 502);
      }
    }

    if (!authed(request, env)) {
      return jsonResp({ error: "unauthorized" }, 401);
    }

    if (path === "/status") return proxyContainer(env, "GET", "/status");

    if (path === "/run-daily" && request.method === "POST") {
      return proxyContainer(env, "POST", "/run-daily", { trigger: "manual" });
    }

    if (path === "/container-restart") {
      try {
        const stub = getContainer(env.WHEEL_CONTAINER, "singleton");
        const resp = await stub.fetch("http://container/__restart", { method: "POST" });
        return new Response(await resp.text(), {
          status: resp.status,
          headers: { "Content-Type": "application/json" },
        });
      } catch (e) {
        return jsonResp({ error: "restart failed", detail: String(e) }, 502);
      }
    }

    return jsonResp({ error: "not found", path }, 404);
  },
};
