import { Notice, TFile } from "obsidian";
import type YoloScribePlugin from "./main";
import { pagePathToVaultPath } from "./sync";

/**
 * Handle a write conflict — called when PUT /obsidian/pages/<path> returns 409.
 *
 * v1 strategy: leave the local file untouched, save the server version as
 * "<name> (remote).md" alongside it, and show a persistent Notice.
 * Full diff/merge UI is deferred to a later iteration (YOL-180).
 */
export async function handleConflict(
	plugin: YoloScribePlugin,
	pagePath: string,
	serverContent: string,
	serverEtag: string
): Promise<void> {
	// Update the stored etag to the server's so subsequent saves use the right base.
	plugin.settings.etagMap[pagePath] = serverEtag;
	await plugin.saveSettings();

	// Write the server version as a sibling "(remote)" file.
	const localPath = pagePathToVaultPath(pagePath);
	const remotePath = localPath.replace(/\.md$/, " (remote).md");

	const existing = plugin.app.vault.getAbstractFileByPath(remotePath);
	if (existing instanceof TFile) {
		await plugin.app.vault.modify(existing, serverContent);
	} else {
		await plugin.app.vault.create(remotePath, serverContent);
	}

	new Notice(
		`YoloScribe conflict on "${pagePath}" — remote version saved as "${remotePath}". ` +
			`Resolve manually and delete one of the two files.`,
		0 // persistent — user must dismiss
	);
}
