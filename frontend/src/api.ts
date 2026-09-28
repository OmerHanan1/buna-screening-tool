export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, options);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail ?? body?.message;
    throw new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item: { msg?: string }) => item.msg || JSON.stringify(item)).join("; ")
          : `Request failed (${response.status}). Please try again.`,
    );
  }
  return response.json() as Promise<T>;
}

export function json(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export function isActive(status: string): boolean {
  return status === "queued" || status === "running";
}

export function hasReport(status: string): boolean {
  return status === "completed" || status === "partial";
}

export function reportAvailable(job: { status: string; report_available?: boolean }): boolean {
  return job.report_available ?? hasReport(job.status);
}

export function safeUrl(value?: string): string | undefined {
  if (!value) return;
  try {
    const url = new URL(value);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch {
    return;
  }
}

export function percent(value: number): string {
  return Number.isFinite(value) ? `${value.toFixed(1)}%` : "—";
}

export function isTestFixture(job: { title: string; is_test_fixture?: boolean }): boolean {
  return job.is_test_fixture === true;
}
