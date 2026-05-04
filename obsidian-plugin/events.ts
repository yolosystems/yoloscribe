import { TFile } from "obsidian";
import type YoloScribePlugin from "./main";
import { deltaSync, pagePathToVaultPath } from "./sync";

const RECONNECT_DELAY_MS = 5000;

/**
 * SSE client for GET /obsidian/events.
 *
 * Uses fetch() + ReadableStream rather than EventSource because EventSource
 * does not support custom request headers (Authorization: Bearer …).
 *
 * On connection drop: waits 5s, runs a deltaSync() catchup to recover any
 * events missed during the outage, then reconnects the stream.
 */
export class SseClient {
	private abortController: AbortController | null = null;
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	private stopped = false;

	constructor(private readonly plugin: YoloScribePlugin) {}

	connect(): void {
		this.stopped = false;
		this.startStream();
	}

	disconnect(): void {
		this.stopped = true;
		if (this.reconnectTimer !== null) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		this.abortController?.abort();
		this.abortController = null;
	}

	private async startStream(): Promise<void> {
		const { apiBaseUrl, apiToken } = this.plugin.settings;
		if (!apiToken || this.stopped) return;

		this.abortController = new AbortController();

		try {
			const resp = await fetch(`${apiBaseUrl}/obsidian/events`, {
				headers: { Authorization: `Bearer ${apiToken}` },
				signal: this.abortController.signal,
			});

			if (!resp.ok || !resp.body) {
				throw new Error(`HTTP ${resp.status}`);
			}

			this.plugin.sseStatus = "connected";
			await this.readStream(resp.body);
		} catch {
			if (this.stopped) return;
			this.scheduleReconnect();
		}
	}

	private async readStream(body: ReadableStream<Uint8Array>): Promise<void> {
		const decoder = new TextDecoder();
		const reader = body.getReader();
		let buffer = "";

		try {
			while (true) {
				const { done, value } = await reader.read();
				if (done) break;
				buffer += decoder.decode(value, { stream: true });

				// SSE messages are separated by a blank line (\n\n).
				const messages = buffer.split("\n\n");
				buffer = messages.pop() ?? "";
				for (const msg of messages) {
					await this.handleMessage(msg);
				}
			}
		} finally {
			reader.releaseLock();
		}

		if (!this.stopped) this.scheduleReconnect();
	}

	private async handleMessage(message: string): Promise<void> {
		const trimmed = message.trim();
		if (!trimmed || trimmed.startsWith(":")) return; // keepalive comment

		let eventType = "message";
		let dataLine = "";

		for (const line of trimmed.split("\n")) {
			if (line.startsWith("event:")) eventType = line.slice(6).trim();
			else if (line.startsWith("data:")) dataLine = line.slice(5).trim();
		}

		if (!dataLine) return;

		let data: Record<string, unknown>;
		try {
			data = JSON.parse(dataLine);
		} catch {
			return; // malformed — ignore
		}

		if (eventType === "page_changed") await this.onPageChanged(data);
		else if (eventType === "page_deleted") await this.onPageDeleted(data);
	}

	private async onPageChanged(
		data: Record<string, unknown>
	): Promise<void> {
		const path = data.path as string | undefined;
		const etag = data.etag as string | undefined;
		if (!path) return;

		// Skip if etag matches our stored value — this is our own write echoed back.
		if (etag && this.plugin.settings.etagMap[path] === etag) return;

		// Use deltaSync to fetch the updated content along with any other pending
		// changes since lastSyncedAt. Best-effort; missed events are recovered on
		// the next reconnect catchup anyway.
		try {
			await deltaSync(this.plugin);
		} catch {
			// swallow — next catchup will cover it
		}
	}

	private async onPageDeleted(
		data: Record<string, unknown>
	): Promise<void> {
		const path = data.path as string | undefined;
		if (!path) return;

		const vaultPath = pagePathToVaultPath(path);
		const file = this.plugin.app.vault.getAbstractFileByPath(vaultPath);
		if (file instanceof TFile) {
			await this.plugin.app.vault.delete(file);
		}
		delete this.plugin.settings.etagMap[path];
		await this.plugin.saveSettings();
	}

	private scheduleReconnect(): void {
		if (this.stopped) return;
		this.plugin.sseStatus = "reconnecting";
		this.reconnectTimer = setTimeout(async () => {
			this.reconnectTimer = null;
			if (this.stopped) return;
			// Catch up on any events missed during the outage.
			try {
				if (this.plugin.settings.lastSyncedAt) {
					await deltaSync(this.plugin);
				}
			} catch {
				// best-effort
			}
			this.startStream();
		}, RECONNECT_DELAY_MS);
	}
}
