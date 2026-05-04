import { Notice, Plugin } from "obsidian";
import {
	DEFAULT_SETTINGS,
	type YoloScribeSettings,
	YoloScribeSettingTab,
} from "./settings";
import { bootstrapSync, deltaSync } from "./sync";
import { registerSaveHandler } from "./save";
import { SseClient } from "./events";

export default class YoloScribePlugin extends Plugin {
	settings: YoloScribeSettings;
	sseStatus: "disconnected" | "connected" | "reconnecting" = "disconnected";
	private sseClient: SseClient | null = null;

	async onload() {
		await this.loadSettings();
		this.addSettingTab(new YoloScribeSettingTab(this.app, this));

		if (this.settings.apiToken) {
			await this.syncOnOpen();
			registerSaveHandler(this);
			this.sseClient = new SseClient(this);
			this.sseClient.connect();
		}
	}

	onunload() {
		this.sseClient?.disconnect();
		this.sseClient = null;
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
