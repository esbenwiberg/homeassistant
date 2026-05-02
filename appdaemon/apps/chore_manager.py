import re
import json
import yaml
import anthropic
import hassapi as hass
from datetime import date, timedelta
from pathlib import Path

FREQ_DAYS = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 91,
    "yearly": 365,
}


class ChoreManager(hass.Hass):

    def initialize(self):
        self.chores_file = Path(self.args["chores_file"])
        self.todo_entity = self.args.get("todo_entity", "todo.chores")
        self.notify_service = self.args.get("notify_service", "notify/notify")
        self.claude = anthropic.Anthropic(api_key=self.args["claude_api_key"])

        self.run_daily(self.daily_refresh, self.args.get("refresh_time", "07:00:00"))
        self.listen_state(self.on_todo_change, self.todo_entity, attribute="all")
        self.listen_event(self.on_add_chore, "chore_add")

        # Delay initial run so HA is fully started
        self.run_in(lambda _: self.recalculate_and_sync(), 10)

    # ── Data ──────────────────────────────────────────────────────────────────

    def load_chores(self):
        with open(self.chores_file) as f:
            return yaml.safe_load(f).get("chores", [])

    def save_chores(self, chores):
        with open(self.chores_file, "w") as f:
            yaml.dump(
                {"chores": chores},
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    # ── Scheduling ────────────────────────────────────────────────────────────

    def baseline_next_due(self, chore):
        freq_days = FREQ_DAYS.get(chore.get("frequency", "weekly"), 7)
        if chore.get("last_completed"):
            last = date.fromisoformat(str(chore["last_completed"]))
            next_due = last + timedelta(days=freq_days)
        else:
            next_due = date.today()
        # Forgiving: if overdue, schedule from today
        return max(next_due, date.today())

    def schedule_with_claude(self, chores):
        today = date.today()
        chore_list = [
            {
                "id": c["id"],
                "name": c["name"],
                "duration_min": c["duration"],
                "frequency": c["frequency"],
                "earliest_date": self.baseline_next_due(c).isoformat(),
            }
            for c in chores
        ]

        prompt = f"""Today is {today.isoformat()} ({today.strftime('%A')}).

Schedule these household chores. Each chore recurs at its stated frequency — give me the next scheduled date for each one.

{json.dumps(chore_list, indent=2)}

Rules:
- Schedule each chore ON or AFTER its earliest_date (never earlier)
- Max 2 chores per day, max 60 combined minutes per day
- Prefer chores over 30min on Saturdays/Sundays
- Spread chores out — avoid clustering

Return ONLY a JSON array, no prose:
[{{"id": "chore_id", "next_due": "YYYY-MM-DD"}}, ...]"""

        resp = self.claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = resp.content[0].text.strip()
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        parsed = json.loads(match.group(0) if match else raw)
        return {item["id"]: item["next_due"] for item in parsed}

    def recalculate_and_sync(self):
        chores = self.load_chores()
        if not chores:
            return

        try:
            schedule = self.schedule_with_claude(chores)
            for chore in chores:
                if chore["id"] in schedule:
                    chore["next_due"] = schedule[chore["id"]]
        except Exception as e:
            self.log(f"Claude scheduling failed, falling back to baseline: {e}", level="WARNING")
            for chore in chores:
                chore["next_due"] = self.baseline_next_due(chore).isoformat()

        self.save_chores(chores)
        self.sync_todo_list(chores)

    # ── HA todo sync ──────────────────────────────────────────────────────────

    def sync_todo_list(self, chores):
        today = date.today().isoformat()
        todays_chores = [c for c in chores if c.get("next_due") == today]

        existing_items = self.get_state(self.todo_entity, attribute="items") or []
        existing_names = {
            item["summary"]
            for item in existing_items
            if item.get("status") != "completed"
        }

        for chore in todays_chores:
            if chore["name"] not in existing_names:
                self.call_service(
                    "todo/add_item",
                    entity_id=self.todo_entity,
                    item=chore["name"],
                    description=f"{chore['duration']}min · {chore['frequency']}",
                )

        # Clean up completed items from previous days
        for item in existing_items:
            if item.get("status") == "completed":
                self.call_service(
                    "todo/remove_item",
                    entity_id=self.todo_entity,
                    item=item["summary"],
                )

    # ── Tick-off detection ────────────────────────────────────────────────────

    def on_todo_change(self, entity, attribute, old, new, kwargs):
        if not old or not new:
            return

        old_items = {
            i["summary"]: i.get("status")
            for i in ((old.get("attributes") or {}).get("items") or [])
        }
        new_items = {
            i["summary"]: i.get("status")
            for i in ((new.get("attributes") or {}).get("items") or [])
        }

        for name, status in new_items.items():
            if status == "completed" and old_items.get(name) != "completed":
                self.log(f"Chore ticked off: {name}")
                self.on_chore_completed(name)

    def on_chore_completed(self, chore_name):
        chores = self.load_chores()
        today = date.today().isoformat()

        for chore in chores:
            if chore["name"] == chore_name:
                skipped = chore.get("skipped_count", 0)
                chore["last_completed"] = today
                chore["skipped_count"] = 0
                self.save_chores(chores)

                peptalk = self.get_peptalk(chore, skipped)
                self.call_service(
                    self.notify_service,
                    title=f"Done: {chore_name}",
                    message=peptalk,
                )

                self.run_in(lambda _: self.recalculate_and_sync(), 2)
                return

    # ── Add chore via HA event ────────────────────────────────────────────────

    def on_add_chore(self, event_name, data, kwargs):
        chores = self.load_chores()

        new_id = re.sub(r"[^a-z0-9]+", "_", data["name"].lower().strip()).strip("_")
        if any(c["id"] == new_id for c in chores):
            new_id = f"{new_id}_{len(chores)}"

        chores.append({
            "id": new_id,
            "name": data["name"].strip(),
            "duration": int(data.get("duration", 30)),
            "frequency": data.get("frequency", "weekly"),
            "last_completed": None,
            "skipped_count": 0,
            "next_due": None,
        })

        self.save_chores(chores)
        self.log(f"Added chore: {data['name']}")
        self.recalculate_and_sync()

    # ── Peptalk ───────────────────────────────────────────────────────────────

    def get_peptalk(self, chore, skipped_count):
        context = (
            f"skipped {skipped_count} time(s) before finally doing it"
            if skipped_count > 0
            else "done right on schedule"
        )
        prompt = (
            f'The user just finished "{chore["name"]}" '
            f'({chore["duration"]}min, {chore["frequency"]}, {context}). '
            f"Give a short, fun, specific peptalk — 1 sentence max. No emojis."
        )

        try:
            resp = self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            self.log(f"Peptalk request failed: {e}", level="WARNING")
            return "Nailed it — one less thing to think about."

    def daily_refresh(self, kwargs):
        self.recalculate_and_sync()
