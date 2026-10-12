import assert from "node:assert/strict";
import test from "node:test";

import { resolveApiBaseUrl } from "./api.ts";

const browser = (protocol, hostname) => ({ protocol, hostname });

test("defaults to the browser hostname and protocol", () => {
  assert.equal(
    resolveApiBaseUrl(undefined, browser("http:", "localhost")),
    "http://localhost:8000",
  );
  assert.equal(
    resolveApiBaseUrl(undefined, browser("https:", "192.168.1.20")),
    "https://192.168.1.20:8000",
  );
});

test("aligns configured loopback hosts with a loopback browser host", () => {
  assert.equal(
    resolveApiBaseUrl(
      "http://127.0.0.1:8000",
      browser("http:", "localhost"),
    ),
    "http://localhost:8000",
  );
  assert.equal(
    resolveApiBaseUrl(
      "http://127.0.0.1:8000",
      browser("http:", "app.example.test"),
    ),
    "http://127.0.0.1:8000",
  );
});

test("preserves an explicitly configured non-loopback API", () => {
  assert.equal(
    resolveApiBaseUrl(
      "https://api.example.test/v1/",
      browser("http:", "localhost"),
    ),
    "https://api.example.test/v1",
  );
});

test("keeps a valid server fallback when no browser exists", () => {
  assert.equal(resolveApiBaseUrl(undefined, null), "http://localhost:8000");
});
