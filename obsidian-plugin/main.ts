import { Plugin } from "obsidian";
import {
	DEFAULT_SETTINGS,
	type YoloScribeSettings,
	YoloScribeSettingTab,
} from "./settings";

export default class YoloScribePlugin extends Plugin {
	settings: YoloScribeSettings;

	async onload() {
		await this.loadSettings();
		this.addSettingTab(new YoloScribeSettingTab(this.app, this));
	}

	onunload() {
		// Sync teardown will be wired here in subsequent issues.
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
}
