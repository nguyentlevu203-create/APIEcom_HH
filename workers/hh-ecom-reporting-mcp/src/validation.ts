// P8.2 - validation layer, ported 1:1 from the P8.0 Python reference
// (mcp_server/hh_ecom_reporting/query_service.py) so semantics match exactly.

export const MAX_ROW_LIMIT = 1000;
export const DEFAULT_ROW_LIMIT = 200;
export const MAX_DATE_RANGE_DAYS = 31;

export const VALID_PLATFORMS = new Set(["SHOPEE", "TIKTOK"]);
export const VALID_ACCOUNT_TYPES = new Set(["ALL", "AFFILIATE_ACCOUNTS"]);

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export class ValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}

function isValidCalendarDate(value: string): boolean {
  const d = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return false;
  return d.toISOString().slice(0, 10) === value;
}

export function validateDate(label: string, value: unknown): string {
  if (typeof value !== "string" || !DATE_RE.test(value) || !isValidCalendarDate(value)) {
    throw new ValidationError(`${label} must be a YYYY-MM-DD date string, got: ${JSON.stringify(value)}`);
  }
  return value;
}

export function validateRange(fromDate: unknown, toDate: unknown): { fromDate: string; toDate: string } {
  const f = validateDate("from_date", fromDate);
  const t = validateDate("to_date", toDate);
  if (f > t) {
    throw new ValidationError("from_date must be on or before to_date");
  }
  const d0 = new Date(`${f}T00:00:00Z`);
  const d1 = new Date(`${t}T00:00:00Z`);
  const days = Math.round((d1.getTime() - d0.getTime()) / 86400000) + 1;
  if (days > MAX_DATE_RANGE_DAYS) {
    throw new ValidationError(
      `date range exceeds the ${MAX_DATE_RANGE_DAYS}-day maximum (${f}..${t} is ${days} days)`
    );
  }
  return { fromDate: f, toDate: t };
}

export function validatePlatform(platform: unknown): string | null {
  if (platform === null || platform === undefined) return null;
  const p = String(platform).trim().toUpperCase();
  if (!VALID_PLATFORMS.has(p)) {
    throw new ValidationError(
      `platform must be one of ${JSON.stringify([...VALID_PLATFORMS].sort())}, got: ${JSON.stringify(platform)}`
    );
  }
  return p;
}

export function validateAccountType(accountType: unknown): string | null {
  if (accountType === null || accountType === undefined) return null;
  const a = String(accountType).trim().toUpperCase();
  if (!VALID_ACCOUNT_TYPES.has(a)) {
    throw new ValidationError(
      `account_type must be one of ${JSON.stringify([...VALID_ACCOUNT_TYPES].sort())}, got: ${JSON.stringify(accountType)}`
    );
  }
  return a;
}

export function validateLimit(limit: unknown): number {
  if (limit === null || limit === undefined) return DEFAULT_ROW_LIMIT;
  const n = Number(limit);
  if (!Number.isInteger(n)) {
    throw new ValidationError(`limit must be an integer, got: ${JSON.stringify(limit)}`);
  }
  if (n <= 0) {
    throw new ValidationError("limit must be a positive integer");
  }
  return Math.min(n, MAX_ROW_LIMIT);
}
