import { Notice, Plugin } from "obsidian";
import {
	DEFAULT_SETTINGS,
	type YoloScribeSettings,
	YoloScribeSettingTab,
} from "./settings";
import { bootstrapSync, deltaSync } from "./sync";

export default class YoloScribePlugin extends Plugin {
	settings: YoloScribeSettings;

	async onload() {
		await this.loadSettings();
		this.addSettingTab(new YoloScribeSettingTab(this.app, this));

		if (this.settings.apiToken) {
			await this.syncOnOpen();
		}
	}

	onunload() {
		// SSE teardown will be wired here in a subsequent issue.
	}

	async loadSettings() {
		this.settings = Object.assign(
			{},
			DEFAULT_SETTINGS,
			await this.loadData()
		);
	}

	async saveSettings() {
		await this.saveData(this.settings);
	}

	async syncOnOpen(): Promise<void> {
		try {
			if (!this.settings.lastSyncedAt) {
				await bootstrapSync(this);
			} else {
				await deltaSync(this);
			}
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			new Notice(`YoloScribe: sync failed — ${msg}. Check settings.`);
		}
	}
}
