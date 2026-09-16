// P8.2 - database access layer. One fixed, parameterized statement per call.
// No caller input is ever concatenated into SQL text. Every DB exception is
// caught and replaced with a generic message before it can reach the client
// (Section 7/17: no DB error, connection string, or host ever leaks out).

import { Client } from "pg";
import { ValidationError } from "./validation";

const STATEMENT_TIMEOUT_MS = 8000;

export interface Env {
  HYPERDRIVE: { connectionString: string };
  MCP_AUTH_TOKEN: string;
}

function toJsonable(row: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(row)) {
    if (v !== null && typeof v === "object" && "toString" in v && (v as { constructor: { name: string } }).constructor?.name === "Decimal") {
      out[k] = Number(v);
    } else if (v instanceof Date) {
      out[k] = v.toISOString().slice(0, 10);
    } else {
      out[k] = v;
    }
  }
  return out;
}

/** Runs exactly one fixed, parameterized statement against Hyperdrive -> Neon hh_ai_reader.
 * Logs only safe metadata (Section 8) via the caller-supplied logger. */
export async function runQuery(
  env: Env,
  toolName: string,
  sql: string,
  params: unknown[],
  paramsSafe: Record<string, unknown>,
  log: (entry: Record<string, unknown>) => void
): Promise<Record<string, unknown>[]> {
  const t0 = Date.now();
  const client = new Client({ connectionString: env.HYPERDRIVE.connectionString });
  try {
    await client.connect();
    await client.query(`SET statement_timeout = ${STATEMENT_TIMEOUT_MS}`);
    const result = await client.query(sql, params);
    const rows = result.rows.map(toJsonable);
    log({
      timestamp: new Date().toISOString(),
      tool_name: toolName,
      params: paramsSafe,
      duration_ms: Date.now() - t0,
      row_count: rows.length,
      outcome: "success",
    });
    return rows;
  } catch (err) {
    if (err instanceof ValidationError) throw err;
    log({
      timestamp: new Date().toISOString(),
      tool_name: toolName,
      params: paramsSafe,
      duration_ms: Date.now() - t0,
      row_count: 0,
      outcome: "failure",
      error_type: err instanceof Error ? err.constructor.name : "Unknown",
    });
    throw new Error("A read-only reporting query failed. No further detail is exposed.");
  } finally {
    try {
      await client.end();
    } catch {
      // connection already closed/failed - nothing further to clean up
    }
  }
}
