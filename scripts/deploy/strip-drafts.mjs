import { lstat, readdir, readFile, unlink } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import matter from "gray-matter";

// Only the disposable production snapshot is changed. The source repository
// and the author's preview retain every draft.
export async function stripDrafts(root) {
	let removed = 0;
	for (const entry of await readdir(root, { withFileTypes: true })) {
		const filename = path.join(root, entry.name);
		if ((await lstat(filename)).isSymbolicLink()) {
			throw new Error("Content snapshot contains a symbolic link");
		}
		if (entry.isDirectory()) {
			removed += await stripDrafts(filename);
		} else if (/\.(md|mdx)$/i.test(entry.name)) {
			const { data } = matter(await readFile(filename, "utf8"));
			if ("draft" in data && typeof data.draft !== "boolean") {
				throw new Error("Draft flag must be a YAML boolean");
			}
			if (data.draft === true) {
				await unlink(filename);
				removed += 1;
			}
		}
	}
	return removed;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	const count = await stripDrafts(path.resolve("src/content"));
	console.log(`Excluded ${count} draft documents from the production snapshot.`);
}
