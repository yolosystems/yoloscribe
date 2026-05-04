import { Notice, TFile } from "obsidian";
import type YoloScribePlugin from "./main";
import { handleConflict } from "./conflicts";
import { vaultPathToPagePath } from "./sync";

const DEBOUNCE_MS = 2000;
const RETRY_DELAY_MS = 5000;

// Pending debounce timers keyed by vault file path.
const _pending = new Map<string, ReturnType<typeof setTimeout>>();

export function registerSaveHandler(plugin: YoloScribePlugin): void {
	plugin.registerEvent(
		plugin.app.vault.on("modify", (file) => {
			if (!(file instanceof TFile) || file.extension !== "md") return;
			scheduleSave(plugin, file);
		})
	);
}

function scheduleSave(plugin: YoloScribePlugin, file: TFile): void {
	const existing = _pending.get(file.path);
	if (existing !== undefined) clearTimeout(existing);
	const timer = setTimeout(() => {
		_pending.delete(file.path);
		pushPage(plugin, file);
	}, DEBOUNCE_MS);
	_pending.set(file.path, timer);
}

async function pushPage(
	plugin: YoloScribePlugin,
	file: TFile,
	isRetry = false
): Promise<void> {
	const pagePath = vaultPathToPagePath(file.path);
	const etag = plugin.settings.etagMap[pagePath];

	// Only push pages originally synced from YoloScribe (i.e. with a known etag).
	if (etag === undefined) return;

	const content = await plugin.app.vault.read(file);
	const { apiBaseUrl, apiToken } = plugin.settings;

	let resp: Response;
	try {
		resp = await fetch(`${apiBaseUrl}/obsidian/pages/${pagePath}`, {
			method: "PUT",
			headers: {
				Authorization: `Bearer ${apiToken}`,
				"Content-Type": "text/markdown",
				"If-Match": etag,
			},
			body: content,
		});
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		if (isRetry) {
			new Notice(`YoloScribe: could not save "${file.basename}" — ${msg}`);
			return;
		}
		new Notice(`YoloScribe: failed to save "${file.basename}" — retrying…`);
		setTimeout(() => pushPage(plugin, file, true), RETRY_DELAY_MS);
		return;
	}

	if (resp.ok) {
		const data = await resp.json();
		plugin.settings.etagMap[pagePath] = data.etag;
		plugin.settings.lastSyncedAt = new Date().toISOString();
		await plugin.saveSettings();
		return;
	}

	if (resp.status === 409) {
		const data = await resp.json();
		await handleConflict(
			plugin,
			pagePath,
			data.content ?? "",
			data.etag ?? ""
		);
		return;
	}

	new Notice(
		`YoloScribe: failed to save "${file.basename}" (${resp.status})`
	);
}
