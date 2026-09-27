/** Run the real sibling CMS REST handlers against its local database.
 * Usage from the cms checkout: pnpm exec tsx <this file> /tmp/notice-cms.json
 * The output file is private and contains this fixture's temporary API key.
 */
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { randomUUID } from "node:crypto";
import { writeFileSync, unlinkSync } from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";

const cms = process.cwd();
process.loadEnvFile(path.join(cms, ".env"));
const db = new URL(process.env.DATABASE_URI || process.env.DATABASE_URL || "");
if (!["localhost", "127.0.0.1"].includes(db.hostname)) throw new Error("Local CMS database required");
const require = createRequire(path.join(cms, "package.json"));
const { getPayload, handleEndpoints } = await import(require.resolve("payload"));
const { default: config } = await import(pathToFileURL(path.join(cms, "src/payload.config.ts")).href);
const payload = await getPayload({ config });
const apiKey = randomUUID();
const service = await payload.create({ collection: "service-users", overrideAccess: true, data: { name: "notice-delivery-integration", enableAPIKey: true, apiKey } });
const output = process.argv[2];
const server = createServer(async (req, res) => {
  try {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    const response = await handleEndpoints({ config, request: new Request(`http://127.0.0.1${req.url}`, { method: req.method, headers: req.headers as Record<string, string>, ...(body.length ? { body } : {}) }) });
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch (error) { console.error(error); res.writeHead(500); res.end(); }
});
server.listen(0, "127.0.0.1", () => {
  const address = server.address();
  writeFileSync(output, JSON.stringify({ url: `http://127.0.0.1:${typeof address === "object" && address?.port}`, api_key: apiKey }), { mode: 0o600 });
  console.log("Notice CMS fixture ready");
});
process.on("SIGTERM", async () => {
  server.close();
  await payload.delete({ collection: "notification-log", where: { "run.run_id": { like: "notice-228-integration-" } }, overrideAccess: true });
  await payload.delete({ collection: "service-users", id: service.id, overrideAccess: true });
  unlinkSync(output);
  await payload.destroy();
  process.exit(0);
});
