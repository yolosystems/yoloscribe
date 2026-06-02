import { Notice, TFile, requestUrl } from "obsidian";
import type YoloScribePlugin from "./main";
import { vaultPathToPagePath } from "./sync";

export function registerIngestHandler(plugin: YoloScribePlugin): void {
	// Fires when a file is created directly in the vault (e.g. Obsidian Web Clipper,
	// Cmd+N while the raw/ folder is selected).
	plugin.registerEvent(
		plugin.app.vault.on("create", (file) => {
			if (!(file instanceof TFile) || file.extension !== "md") return;
			maybeIngest(plugin, file);
		})
	);

	// Fires when a file is moved or renamed — covers drag-and-drop and
	// "Move file to..." into the ingest folder.
	plugin.registerEvent(
		plugin.app.vault.on("rename", (file, oldPath) => {
			if (!(file instanceof TFile) || file.extension !== "md") return;
			const { ingestFolder } = plugin.settings;
			if (!ingestFolder) return;
			const folder = ingestFolder.replace(/\/$/, "");
			const oldPagePath = vaultPathToPagePath(oldPath);
			// Only ingest if the file moved INTO the folder, not within it.
			const wasAlreadyInFolder =
				oldPagePath === folder || oldPagePath.startsWith(folder + "/");
			if (!wasAlreadyInFolder) maybeIngest(plugin, file);
		})
	);
}

function maybeIngest(plugin: YoloScribePlugin, file: TFile): void {
	const { ingestFolder } = plugin.settings;
	if (!ingestFolder) return;

	const folder = ingestFolder.replace(/\/$/, "");
	const pagePath = vaultPathToPagePath(file.path);

	const isInFolder = pagePath === folder || pagePath.startsWith(folder + "/");
	if (!isInFolder) return;

	// Ingest always lands at the single .user/ingest queue page — the local
	// filename is irrelevant to the remote destination.
	const remotePath = ".user/ingest";

	// Guard: slugification may reduce a name to nothing (e.g. all special chars).
	if (!pagePath || !/^[a-z0-9]/.test(pagePath)) {
		new Notice(
			`YoloScribe: can't ingest "${file.basename}" — rename it to start with a letter or digit`
		);
		return;
	}

	pushIngestPage(plugin, file, remotePath);
}

async function pushIngestPage(
	plugin: YoloScribePlugin,
	file: TFile,
	pagePath: string
): Promise<void> {
	const content = await plugin.app.vault.read(file);
	const { apiBaseUrl, apiToken } = plugin.settings;

	let status: number;
	try {
		const resp = await requestUrl({
			url: `${apiBaseUrl}/obsidian/pages/${pagePath}`,
			method: "PUT",
			headers: {
				Authorization: `Bearer ${apiToken}`,
				"Content-Type": "text/markdown",
			},
			body: content,
			throw: false,
		});
		status = resp.status;
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		new Notice(`YoloScribe: failed to ingest "${file.basename}" — ${msg}`);
		return;
	}

	if (status >= 200 && status < 300) {
		new Notice(`YoloScribe: ingested "${file.basename}"`);
		return;
	}

	new Notice(
		`YoloScribe: failed to ingest "${file.basename}" (${status})`
	);
}
