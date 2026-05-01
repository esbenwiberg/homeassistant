# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This repository contains custom automations, scripts, and configurations for a Home Assistant setup.

## Repository Structure

This repo is in early development. As it grows, expect to find:

- `automations/` — YAML automation definitions
- `scripts/` — custom scripts (Python, shell, or YAML)
- `blueprints/` — reusable automation blueprints
- `custom_components/` — custom Home Assistant integrations
- `appdaemon/` — AppDaemon apps (if used)

## Home Assistant Specifics

- Automations and scripts are written in YAML and follow the [Home Assistant schema](https://www.home-assistant.io/docs/automation/)
- Custom integrations live under `custom_components/<integration_name>/` and require `manifest.json`, `__init__.py`, and any platform files
- AppDaemon apps are Python classes extending `appdaemon.plugins.hass.hassapi.Hass`
