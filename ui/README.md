# Mudrex Monitor UI

Live, read-only dashboard for the Mudrex S1 bot, served by the bot itself: double-click `start-dashboard.cmd` in
the mudrex-bot folder and open http://127.0.0.1:8765. One process (`python dashboard.py`) serves this UI's static
build (`ui/dist/client`) and its data at `/api/ui` (watcher status, execution journal, read-only Mudrex calls).
This page places no orders; approvals stay in Telegram.

After changing anything in `src/`, rebuild: `npx vite build` in this folder. For live editing, run
`python dashboard.py` and `npx vite dev` (port 8080, `/api` is proxied to the bot).
`src/data/mudrex-demo.json` is only the type template for the `/api/ui` shape.

## Customize the preview data

Edit [`src/data/mudrex-demo.json`](src/data/mudrex-demo.json) to change the sample dashboard. This file is the single source for:

- Summary metric cards
- Equity chart points for the `1D`, `7D`, and `30D` ranges
- Market regime and BTC context
- Daily risk-cap display
- Strategy plan and strategy settings
- Open positions, activity events, notifications, and display timestamps

Keep the JSON valid (double quotes around keys and string values, commas between items). The `activity[].icon` and `notifications[].icon` fields should use a supported icon key: `Activity`, `ShieldAlert`, `ShieldCheck`, or `TrendingUp`. Position protection `state` values are `Protected` or `Stop pending`; activity `tone` values are `green`, `blue`, `amber`, or `gray`; plan `tone` values are `hold`, `review`, or `idle`. Supported `activity[].icon` and `notifications[].icon` values are `Activity`, `ShieldAlert`, `ShieldCheck`, and `TrendingUp`.

After saving the JSON file, Vite reloads the page during development. The refresh button in the dashboard only updates its local “preview updated” time; it does not fetch or reload exchange data.

## Development

This project uses Bun, Vite, TanStack Start, React, and TypeScript.

```sh
bun install
bun run dev
```

## Checks

```sh
bun run lint
bun run build
```

## Preview safety

The displayed positions, P&L, chart, market regime, plan, and activity are demo content. No exchange credentials or API calls are used. Do not interpret sample values as live account data or investment guidance.
