import { App, Notice, PluginSettingTab, Setting, requestUrl } from "obsidian";
import type YoloScribePlugin from "./main";

export interface YoloScribeSettings {
	apiBaseUrl: string;
	apiToken: string;
	site: string;
	subtree: string;
	ingestFolder: string;
	syncIntervalSeconds: number;
	lastSyncedAt: string;
	etagMap: Record<string, string>;
}

export const DEFAULT_SETTINGS: YoloScribeSettings = {
	apiBaseUrl: "https://app.yoloscribe.com",
	apiToken: "",
	site: "",
	subtree: "",
	ingestFolder: "raw/",
	syncIntervalSeconds: 30,
	lastSyncedAt: "",
	etagMap: {},
};

export class YoloScribeSettingTab extends PluginSettingTab {
	plugin: YoloScribePlugin;

	constructor(app: App, plugin: YoloScribePlugin) {
		super(app, plugin);
		this.plugin = plugin;
	}

	display(): void {
		const { containerEl } = this;
		containerEl.empty();

		new Setting(containerEl)
			.setName("API Base URL")
			.setDesc(
				"Your YoloScribe instance URL " +
				"(e.g. https://app.yoloscribe.com or http://localhost:8000)"
			)
			.addText((text) =>
				text
					.setPlaceholder("https://app.yoloscribe.com")
					.setValue(this.plugin.settings.apiBaseUrl)
					.onChange(async (value) => {
						this.plugin.settings.apiBaseUrl = value.replace(/\/$/, "");
						await this.plugin.saveSettings();
					})
			);

		// Token field + Verify button share a status element rendered below.
		let verifyStatusEl: HTMLElement;

		new Setting(containerEl)
			.setName("API Token")
			.setDesc("Generate a token in YoloScribe → Settings → API Tokens.")
			.addText((text) => {
				text
					.setPlaceholder("as_...")
					.setValue(this.plugin.settings.apiToken)
					.onChange(async (value) => {
						this.plugin.settings.apiToken = value.trim();
						// Clear resolved site when the token changes.
						this.plugin.settings.site = "";
						await this.plugin.saveSettings();
					});
				text.inputEl.type = "password";
				return text;
			})
			.addButton((btn) =>
				btn
					.setButtonText("Verify")
					.setCta()
					.onClick(async () => {
						await this.verifyToken(verifyStatusEl);
					})
			);

		verifyStatusEl = containerEl.createEl("p", {
			cls: "yoloscribe-verify-status",
			text: this.plugin.settings.site
				? `✓ Connected to site: ${this.plugin.settings.site}`
				: "",
		});

		new Setting(containerEl)
			.setName("Sync scope (optional)")
			.setDesc(
				"Limit sync to a page subtree, e.g. projects/myproject. " +
				"Leave empty to sync the entire site."
			)
			.addText((text) =>
				text
					.setPlaceholder("projects/myproject")
					.setValue(this.plugin.settings.subtree)
					.onChange(async (value) => {
						this.plugin.settings.subtree = value.trim();
						await this.plugin.saveSettings();
					})
			);

		new Setting(containerEl)
			.setName("Ingest folder")
			.setDesc(
				"New files created in this vault folder are automatically pushed " +
				"to YoloScribe as new pages. Use with Obsidian Web Clipper pointed " +
				"at this folder. Leave empty to disable."
			)
			.addText((text) =>
				text
					.setPlaceholder("raw/")
					.setValue(this.plugin.settings.ingestFolder)
					.onChange(async (value) => {
						this.plugin.settings.ingestFolder = value.trim();
						await this.plugin.saveSettings();
					})
			);

		new Setting(containerEl)
			.setName("Polling interval (seconds)")
			.setDesc(
				"How often to check for remote changes when the live " +
				"connection is unavailable. Minimum 5."
			)
			.addText((text) =>
				text
					.setPlaceholder("30")
					.setValue(String(this.plugin.settings.syncIntervalSeconds))
					.onChange(async (value) => {
						const n = parseInt(value, 10);
						if (!isNaN(n) && n >= 5) {
							this.plugin.settings.syncIntervalSeconds = n;
							await this.plugin.saveSettings();
						}
					})
			);

		if (this.plugin.settings.apiToken) {
			new Setting(containerEl)
				.setName("Disconnect")
				.setDesc("Clear the API token and reset all sync state.")
				.addButton((btn) =>
					btn
						.setButtonText("Disconnect")
						.setWarning()
						.onClick(async () => {
							this.plugin.settings.apiToken = "";
							this.plugin.settings.site = "";
							this.plugin.settings.lastSyncedAt = "";
							this.plugin.settings.etagMap = {};
							await this.plugin.saveSettings();
							new Notice("YoloScribe: disconnected");
							this.display();
						})
				);
		}
	}

	private async verifyToken(statusEl: HTMLElement): Promise<void> {
		const { apiBaseUrl, apiToken } = this.plugin.settings;
		if (!apiToken) {
			statusEl.setText("Enter an API token first.");
			return;
		}
		statusEl.setText("Verifying…");
		try {
			const resp = await requestUrl({
				url: `${apiBaseUrl}/obsidian/status`,
				headers: { Authorization: `Bearer ${apiToken}` },
				throw: false,
			});
			if (resp.status < 200 || resp.status >= 300) {
				statusEl.setText(
					`✗ Error ${resp.status} — check your token and base URL.`
				);
				return;
			}
			const data = resp.json;
			this.plugin.settings.site = data.site ?? "";
			await this.plugin.saveSettings();
			statusEl.setText(
				`✓ Connected to site: ${data.site} (${data.page_count} pages)`
			);
			await this.plugin.activate();
		} catch {
			statusEl.setText(
				`✗ Could not reach ${apiBaseUrl} — check the base URL.`
			);
		}
	}
}
