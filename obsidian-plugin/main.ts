import { Plugin } from "obsidian";

export default class YoloScribePlugin extends Plugin {
	async onload() {
		console.log("YoloScribe plugin loaded");
	}

	onunload() {
		console.log("YoloScribe plugin unloaded");
	}
}
