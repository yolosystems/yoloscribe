import { Notice, TFile, requestUrl } from "obsidian";
import type YoloScribePlugin from "./main";
import { handleConflict } from "./conflicts";
import { vaultPathToPagePath } from "./sync";

const DEBOUNCE_MS = 2000;

/** Remove the %% yoloscribe-child-pages ... %% block appended by the backend. */
function stripChildPagesBlock(content: string): string {
	return content.replace(/\n+%% yoloscribe-child-pages\n[\s\S]*?\n%%/g, "").trimEnd();
}
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

	const rawContent = await plugin.app.vault.read(file);
	const content = stripChildPagesBlock(rawContent);
	const { apiBaseUrl, apiToken } = plugin.settings;

	let status: number;
	let json: Record<string, unknown>;
	try {
		const resp = await requestUrl({
			url: `${apiBaseUrl}/obsidian/pages/${pagePath}`,
			method: "PUT",
			headers: {
				Authorization: `Bearer ${apiToken}`,
				"Content-Type": "text/markdown",
				"If-Match": etag,
			},
			body: content,
			throw: false,
		});
		status = resp.status;
		json = resp.json as Record<string, unknown>;
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

	if (status >= 200 && status < 300) {
		plugin.settings.etagMap[pagePath] = json.etag as string;
		plugin.settings.lastSyncedAt = new Date().toISOString();
		await plugin.saveSettings();
		return;
	}

	if (status === 409) {
		await handleConflict(
			plugin,
			pagePath,
			(json.content as string) ?? "",
			(json.etag as string) ?? ""
		);
		return;
	}

	new Notice(
		`YoloScribe: failed to save "${file.basename}" (${status})`
	);
}
