import { App, Notice, TFile, normalizePath, requestUrl } from "obsidian";
import type YoloScribePlugin from "./main";

// ── Path mapping ───────────────────────────────────────────────────────────────

/**
 * Convert a YoloScribe page path to a vault file path.
 *   "projects/yoloscribe/ideas" → "projects/yoloscribe/ideas.md"
 * Root page (empty string) is skipped — see syncOnOpen for rationale.
 */
export function pagePathToVaultPath(pagePath: string): string {
	return normalizePath(`${pagePath}.md`);
}

function slugifySegment(seg: string): string {
	return seg
		.toLowerCase()
		.replace(/\s+/g, "-")
		.replace(/[^a-z0-9_-]/g, "")
		.replace(/-+/g, "-")
		.replace(/^-+|-+$/g, "");
}

/**
 * Convert a vault file path to a YoloScribe page path, slugifying each
 * segment so that Obsidian note names with spaces or uppercase map to valid
 * YoloScribe page paths.
 *   "projects/yoloscribe/ideas.md" → "projects/yoloscribe/ideas"
 *   "raw/Another new note.md"      → "raw/another-new-note"
 */
export function vaultPathToPagePath(vaultPath: string): string {
	const withoutExt = vaultPath.replace(/\.md$/, "");
	return withoutExt
		.split("/")
		.map(slugifySegment)
		.filter((seg) => seg.length > 0)
		.join("/");
}

// ── Vault helpers ──────────────────────────────────────────────────────────────

async function ensureParentDir(app: App, filePath: string): Promise<void> {
	const dir = filePath.split("/").slice(0, -1).join("/");
	if (!dir) return;
	await app.vault.adapter.mkdir(normalizePath(dir));
}

async function writePage(
	app: App,
	vaultPath: string,
	content: string
): Promise<void> {
	const existing = app.vault.getAbstractFileByPath(vaultPath);
	if (existing instanceof TFile) {
		await app.vault.modify(existing, content);
	} else {
		await ensureParentDir(app, vaultPath);
		await app.vault.create(vaultPath, content);
	}
}

async function deletePage(app: App, vaultPath: string): Promise<void> {
	const file = app.vault.getAbstractFileByPath(vaultPath);
	if (file instanceof TFile) {
		await app.vault.delete(file);
	}
}

// ── API fetch ──────────────────────────────────────────────────────────────────

async function apiFetch(
	plugin: YoloScribePlugin,
	path: string
): Promise<{ status: number; json: unknown }> {
	const { apiBaseUrl, apiToken } = plugin.settings;
	const resp = await requestUrl({
		url: `${apiBaseUrl}${path}`,
		headers: { Authorization: `Bearer ${apiToken}` },
		throw: false,
	});
	return { status: resp.status, json: resp.json };
}

// ── Sync operations ────────────────────────────────────────────────────────────

/**
 * Bootstrap sync — called on first vault open (no lastSyncedAt).
 * Fetches all pages in one request and writes them to the vault.
 * Root page (path === "") is skipped; it has no stable vault filename.
 */
export async function bootstrapSync(plugin: YoloScribePlugin): Promise<void> {
	const { subtree } = plugin.settings;
	const qs = subtree ? `?subtree=${encodeURIComponent(subtree)}` : "";
	const resp = await apiFetch(plugin, `/obsidian/bootstrap${qs}`);

	if (resp.status < 200 || resp.status >= 300) {
		throw new Error(`HTTP ${resp.status}`);
	}

	const data = resp.json as {
		site?: string;
		synced_at: string;
		pages: Array<{ path: string; content: string; etag: string; updated_at: string }>;
	};
	const pages = data.pages ?? [];

	let written = 0;
	for (const page of pages) {
		if (page.path === "") continue; // skip root page
		// Set etag BEFORE writePage so vault.on('create') can detect sync-owned files.
		plugin.settings.etagMap[page.path] = page.etag;
		const vaultPath = pagePathToVaultPath(page.path);
		await writePage(plugin.app, vaultPath, page.content);
		written++;
	}

	plugin.settings.site = data.site ?? plugin.settings.site;
	plugin.settings.lastSyncedAt = data.synced_at;
	await plugin.saveSettings();

	new Notice(`YoloScribe: synced ${written} page${written === 1 ? "" : "s"}`);
}

/**
 * Delta sync — called on subsequent vault opens.
 * Fetches only pages changed since lastSyncedAt and applies the diff.
 * Skips pages with updated_by === "obsidian" — the plugin made those writes.
 */
export async function deltaSync(plugin: YoloScribePlugin): Promise<void> {
	const since = encodeURIComponent(plugin.settings.lastSyncedAt);
	const resp = await apiFetch(plugin, `/obsidian/changes?since=${since}`);

	if (resp.status < 200 || resp.status >= 300) {
		throw new Error(`HTTP ${resp.status}`);
	}

	const data = resp.json as {
		synced_at: string;
		changed: Array<{ path: string; content: string; etag: string; updated_by?: string }>;
		deleted: string[];
	};
	const changed = data.changed ?? [];
	const deleted: string[] = data.deleted ?? [];

	let applied = 0;
	for (const page of changed) {
		if (page.path === "") continue;
		// Skip our own writes — etag already matches what we stored.
		if (plugin.settings.etagMap[page.path] === page.etag) continue;
		// Set etag BEFORE writePage so vault.on('create') can detect sync-owned files.
		plugin.settings.etagMap[page.path] = page.etag;
		const vaultPath = pagePathToVaultPath(page.path);
		await writePage(plugin.app, vaultPath, page.content);
		applied++;
	}

	for (const pagePath of deleted) {
		if (pagePath === "") continue;
		await deletePage(plugin.app, pagePathToVaultPath(pagePath));
		delete plugin.settings.etagMap[pagePath];
	}

	plugin.settings.lastSyncedAt = data.synced_at;
	await plugin.saveSettings();

	if (applied > 0 || deleted.length > 0) {
		new Notice(
			`YoloScribe: applied ${applied} update${applied === 1 ? "" : "s"}` +
				(deleted.length > 0 ? `, removed ${deleted.length}` : "")
		);
	}
}
