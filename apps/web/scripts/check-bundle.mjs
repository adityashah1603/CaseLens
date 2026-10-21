// Fails when a production bundle (dist/) contains a JWT-looking string ("eyJ...") or the value of
// any VITE_DEV_TOKEN_* from .env / .env.local. Run after `pnpm build`: `pnpm check:bundle`.
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const dist = join(root, "dist");
if (!existsSync(dist)) {
  console.error("check-bundle: dist/ does not exist; run `pnpm build` first");
  process.exit(2);
}

const secrets = [];
for (const name of [".env", ".env.local", ".env.production", ".env.production.local"]) {
  const file = join(root, name);
  if (!existsSync(file)) continue;
  for (const line of readFileSync(file, "utf8").split(/\r?\n/)) {
    const match = /^\s*(VITE_DEV_TOKEN_[A-Z_]+)\s*=\s*(.*?)\s*$/.exec(line);
    if (match && match[2] !== "") secrets.push({ name: match[1], value: match[2] });
  }
}

const files = [];
const walk = (dir) => {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path);
    else files.push(path);
  }
};
walk(dist);

const problems = [];
for (const file of files) {
  const text = readFileSync(file, "latin1");
  if (/eyJ[A-Za-z0-9_-]{8,}/.test(text)) problems.push(`${file}: contains a JWT-looking "eyJ..." string`);
  for (const { name, value } of secrets) {
    if (text.includes(value)) problems.push(`${file}: contains the value of ${name}`);
  }
}

if (problems.length > 0) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log(`check-bundle: ${files.length} files in dist/, no dev tokens (${secrets.length} checked)`);
