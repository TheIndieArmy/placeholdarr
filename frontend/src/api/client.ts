export async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    headers: {
      "Accept": "application/json",
    },
  });

  if (!response.ok) {
    let message = `Request failed: ${response.status}`;
    try {
      const payload = (await response.json()) as { message?: string; detail?: string };
      message = payload.message || payload.detail || message;
    } catch {
      // Keep fallback message when response is not JSON.
    }
    throw new Error(message);
  }

  return (await response.json()) as T;
}
