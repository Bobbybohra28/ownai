import { ApiError, api, setAccessToken } from "./client";

function mockFetch(status: number, body: string) {
  return vi.fn().mockImplementation(async () => new Response(body, { status, headers: { "Content-Type": "application/json" } }));
}

describe("api client", () => {
  beforeEach(() => setAccessToken("token"));

  it("normalises backend errors", async () => {
    globalThis.fetch = mockFetch(502, JSON.stringify({ error: { code: "MODEL_EMPTY_RESPONSE", message: "empty", hint: "h" } }));
    await expect(api("/x")).rejects.toMatchObject({ code: "MODEL_EMPTY_RESPONSE", hint: "h" });
  });

  it("treats an empty 200 body as FRONTEND_RESPONSE_ERROR", async () => {
    globalThis.fetch = mockFetch(200, "");
    await expect(api("/x")).rejects.toBeInstanceOf(ApiError);
    await expect(api("/x")).rejects.toMatchObject({ code: "FRONTEND_RESPONSE_ERROR" });
  });

  it("rejects invalid JSON", async () => {
    globalThis.fetch = mockFetch(200, "<html>");
    await expect(api("/x")).rejects.toMatchObject({ code: "FRONTEND_RESPONSE_ERROR" });
  });

  it("sends the bearer token", async () => {
    const f = mockFetch(200, "{}");
    globalThis.fetch = f;
    await api("/y");
    expect(f.mock.calls[0][1].headers.Authorization).toBe("Bearer token");
  });
});
