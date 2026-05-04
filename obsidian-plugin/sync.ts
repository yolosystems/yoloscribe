import { App, Notice, TFile, normalizePath } from "obsidian";
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

/**
 * Convert a vault file path back to a YoloScribe page path.
 *   "projects/yoloscribe/ideas.md" → "projects/yoloscribe/ideas"
 */
export function vaultPathToPagePath(vaultPath: string): string {
	return vaultPath.replace(/\.md$/, "");
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
): Promise<Response> {
	const { apiBaseUrl, apiToken } = plugin.settings;
	return fetch(`${apiBaseUrl}${path}`, {
		headers: { Authorization: `Bearer ${apiToken}` },
	});
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

	if (!resp.ok) {
		throw new Error(`${resp.status} ${resp.statusText}`);
	}

	const data = await resp.json();
	const pages: Array<{
		path: string;
		content: string;
		etag: string;
		updated_at: string;
	}> = data.pages ?? [];

	let written = 0;
	for (const page of pages) {
		if (page.path === "") continue; // skip root page
		const vaultPath = pagePathToVaultPath(page.path);
		await writePage(plugin.app, vaultPath, page.content);
		plugin.settings.etagMap[page.path] = page.etag;
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

	if (!resp.ok) {
		throw new Error(`${resp.status} ${resp.statusText}`);
	}

	const data = await resp.json();
	const changed: Array<{
		path: string;
		content: string;
		etag: string;
		updated_by?: string;
	}> = data.changed ?? [];
	const deleted: string[] = data.deleted ?? [];

	let applied = 0;
	for (const page of changed) {
		if (page.path === "") continue;
		// Skip our own writes — etag already matches what we stored.
		if (plugin.settings.etagMap[page.path] === page.etag) continue;
		const vaultPath = pagePathToVaultPath(page.path);
		await writePage(plugin.app, vaultPath, page.content);
		plugin.settings.etagMap[page.path] = page.etag;
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
