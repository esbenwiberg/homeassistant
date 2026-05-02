# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This repository contains custom automations, scripts, and configurations for a Home Assistant setup.

## Repository Structure

- `chores/chores.yaml` — chore definitions (source of truth, human-editable)
- `appdaemon/apps/chore_manager.py` — AppDaemon app managing chore scheduling and tick-off
- `appdaemon/apps/apps.yaml` — AppDaemon app configuration (API keys, entity names)
- `ha_config/chores_helpers.yaml` — HA input helpers and script for adding chores
- `lovelace/chores_dashboard.yaml` — Lovelace dashboard for today's chores and adding new ones

As the repo grows, also expect:
- `automations/` — YAML automation definitions
- `blueprints/` — reusable automation blueprints
- `custom_components/` — custom HA integrations

## Chore Manager

The chore system is an AppDaemon app (`ChoreManager`) backed by `chores/chores.yaml`.

**Data model** — each chore has: `id`, `name`, `duration` (minutes), `frequency` (`daily` / `weekly` / `biweekly` / `monthly` / `quarterly` / `yearly`), `kids_ok` (bool), `assigned_to` (family member name, derived), `last_completed` (ISO date or null), `skipped_count`, `next_due` (ISO date, derived).

**Family members** — configured in `apps.yaml` under `family`. Each member has a `name`, `role` (`adult` or `kid`), `todo_entity` (a separate HA `local_todo` entity), and optionally a `notify_service`. Adults can be assigned any chore; kids only get chores flagged `kids_ok: true`. Claude aims to give each person roughly one chore per day.

**Scheduling** — on startup and daily at the configured `refresh_time`, the app calls Claude (`claude-sonnet-4-6`) to distribute all chores across the calendar, assign each to a family member, respecting a max of 2 chores / 60 min per person per day and preferring heavy chores on weekends. Falls back to a simple `last_completed + frequency_days` + round-robin assignment baseline if the API call fails.

**Tick-off** — the app watches each member's `todo.*` HA entity via `listen_state`. When an item flips to `completed`, it updates `last_completed` in the YAML, calls Claude (`claude-haiku-4-5-20251001`) for a personalized one-sentence peptalk, sends a push notification to that member, and recalculates the schedule.

**Adding a chore** — fire the `chore_add` HA event with `name`, `duration`, `frequency`, and `kids_ok`. The Lovelace dashboard + `script.add_chore` do this from the UI.

**Forgiving scheduling** — if a chore is overdue, `next_due` is set to today (never guilt-stacked into the past).

**Dashboard** — `lovelace/chores_dashboard.yaml` renders a 2×2 grid of `todo-list` cards, one per family member, with color-coded borders (requires the `card-mod` HACS integration for colors; works without it).

## Home Assistant Specifics

- AppDaemon apps are Python classes extending `appdaemon.plugins.hass.hassapi.Hass`
- Automations and scripts follow the [Home Assistant schema](https://www.home-assistant.io/docs/automation/)
- Custom integrations live under `custom_components/<name>/` and require `manifest.json` + `__init__.py`
- The `chores_file` path in `apps.yaml` must be an absolute path accessible from the AppDaemon container (typically `/config/...`)
