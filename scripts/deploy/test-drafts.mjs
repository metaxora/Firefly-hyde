import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { stripDrafts } from "./strip-drafts.mjs";

const root = await mkdtemp(path.join(os.tmpdir(), "firefly-draft-test-"));
try {
	await mkdir(path.join(root, "nested"));
	await writeFile(path.join(root, "published.md"), "---\ntitle: Published\ndraft: false\n---\nPublic content");
	await writeFile(path.join(root, "normal.md"), "---\ntitle: Normal\n---\nPublic content");
	await writeFile(path.join(root, "draft.md"), "---\ntitle: Private\ndraft: true # unpublished\n---\nPRIVATE_DRAFT_SENTINEL");
	await writeFile(path.join(root, "nested", "draft.mdx"), "---\ntitle: Private MDX\ndraft: true\n---\nPRIVATE_DRAFT_SENTINEL");
	assert.equal(await stripDrafts(root), 2);
	await assert.rejects(readFile(path.join(root, "draft.md")), { code: "ENOENT" });
	await assert.rejects(readFile(path.join(root, "nested", "draft.mdx")), { code: "ENOENT" });
	assert.match(await readFile(path.join(root, "published.md"), "utf8"), /Public content/);
	assert.match(await readFile(path.join(root, "normal.md"), "utf8"), /Public content/);
	await writeFile(path.join(root, "invalid.md"), '---\ndraft: "true"\n---\nContent');
	await assert.rejects(stripDrafts(root), /YAML boolean/);
	console.log("Draft privacy checks passed: Markdown, MDX, comments, published documents and invalid flags.");
} finally {
	assert.equal(path.dirname(root), path.resolve(os.tmpdir()));
	assert.ok(path.basename(root).startsWith("firefly-draft-test-"));
	await rm(root, { recursive: true, force: true });
}
