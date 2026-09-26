import { parseSSE } from "./sse";

describe("parseSSE", () => {
  it("parses complete events and keeps the partial remainder", () => {
    const input = 'id: 1\nevent: status\ndata: {"message":"Planning"}\n\n: keep-alive\n\nid: 2\nevent: tok';
    const { events, rest } = parseSSE(input);
    expect(events).toEqual([{ id: 1, type: "status", data: { message: "Planning" } }]);
    expect(rest).toBe("id: 2\nevent: tok");
  });

  it("reports malformed JSON as a frontend error event instead of dropping it", () => {
    const { events } = parseSSE("id: 3\nevent: token\ndata: {broken\n\n");
    expect(events[0].type).toBe("error");
    expect(events[0].data.code).toBe("FRONTEND_RESPONSE_ERROR");
  });
});
