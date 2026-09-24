/**
 * pi-daemon — settings dock view.
 *
 * One responsibility: render the presenter's rows through pi's own
 * two-column SettingsList (padded label | current value, the selected
 * row's description below) framed by DynamicBorder, and forward keyboard
 * input to the list. Pure presentation: it owns no persistence and
 * reports accepted in-place changes to the caller's callback. Search is
 * omitted to match the built-in /settings.
 */

import {
	Container,
	SettingsList,
	type SettingItem,
	type SettingsListTheme,
} from "@earendil-works/pi-tui";
import { DynamicBorder } from "@earendil-works/pi-coding-agent";
import type { SettingRow, SettingsChange, ViewTheme } from "./presenter.ts";

export class DaemonSettingsView extends Container {
	private readonly list: SettingsList;

	constructor(
		rows: readonly SettingRow[],
		theme: ViewTheme,
		onChange: SettingsChange,
		onCancel: () => void,
	) {
		super();
		const border = (text: string): string => theme.fg("border", text);
		this.list = new SettingsList(
			rows.map((row) => DaemonSettingsView.toItem(row)),
			rows.length, // every row stays visible; 14 rows fits a pane.
			DaemonSettingsView.themeFor(theme),
			onChange,
			onCancel,
		);
		this.addChild(new DynamicBorder(border));
		this.addChild(this.list);
		this.addChild(new DynamicBorder(border));
	}

	/** Forward focus input to the list; the Container base has none. */
	handleInput(data: string): void {
		this.list.handleInput(data);
	}

	/** Move the selection to a row id (programmatic navigation). */
	selectItem(id: string): void {
		this.list.selectItem(id);
	}

	/** One SettingItem: flags carry cycle values, others a submenu. */
	private static toItem(row: SettingRow): SettingItem {
		const item: SettingItem = {
			id: row.id,
			label: row.label,
			description: row.description,
			currentValue: row.value,
		};
		if (row.submenu) item.submenu = row.submenu;
		else if (row.values) item.values = row.values;
		return item;
	}

	/** pi's settings palette, built from the injected theme (jiti-safe). */
	private static themeFor(theme: ViewTheme): SettingsListTheme {
		return {
			label: (text, selected) =>
				selected ? theme.fg("accent", text) : text,
			value: (text, selected) =>
				selected ? theme.fg("accent", text) : theme.fg("muted", text),
			description: (text) => theme.fg("dim", text),
			cursor: theme.fg("accent", "→ "),
			hint: (text) => theme.fg("dim", text),
		};
	}
}
