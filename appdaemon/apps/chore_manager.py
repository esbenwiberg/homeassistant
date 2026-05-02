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
        self.family = self.args.get("family", [])
        self.global_notify = self.args.get("notify_service", "notify/notify")
        self.claude = anthropic.Anthropic(api_key=self.args["claude_api_key"])

        self.run_daily(self.daily_refresh, self.args.get("refresh_time", "07:00:00"))

        for member in self.family:
            self.listen_state(
                self.on_todo_change,
                member["todo_entity"],
                attribute="all",
                member=member,
            )

        self.listen_event(self.on_add_chore, "chore_add")
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
        return max(next_due, date.today())

    def schedule_with_claude(self, chores):
        today = date.today()

        family_lines = "\n".join(
            f"- {m['name']} ({'can do all chores' if m.get('role') == 'adult' else 'kids_ok chores only'})"
            for m in self.family
        )
        chore_list = [
            {
                "id": c["id"],
                "name": c["name"],
                "duration_min": c["duration"],
                "frequency": c["frequency"],
                "kids_ok": c.get("kids_ok", False),
                "earliest_date": self.baseline_next_due(c).isoformat(),
            }
            for c in chores
        ]

        prompt = f"""Today is {today.isoformat()} ({today.strftime('%A')}).

Schedule household chores and assign each to a family member.

Family:
{family_lines}

Chores:
{json.dumps(chore_list, indent=2)}

Rules:
- Schedule each chore ON or AFTER its earliest_date (never earlier)
- Assign each chore to exactly one family member
- Kids can only be assigned chores where kids_ok is true
- Aim for each person to have roughly one chore per day
- Max 2 chores / 60 combined minutes per person per day
- Prefer chores over 30min on Saturdays/Sundays
- Distribute load fairly between adults

Return ONLY a JSON array, no prose:
[{{"id": "chore_id", "next_due": "YYYY-MM-DD", "assigned_to": "Name"}}, ...]"""

        resp = self.claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = resp.content[0].text.strip()
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        parsed = json.loads(match.group(0) if match else raw)
        return {
            item["id"]: {
                "next_due": item["next_due"],
                "assigned_to": item.get("assigned_to"),
            }
            for item in parsed
        }

    def recalculate_and_sync(self):
        chores = self.load_chores()
        if not chores:
            return

        try:
            schedule = self.schedule_with_claude(chores)
            for chore in chores:
                if chore["id"] in schedule:
                    chore["next_due"] = schedule[chore["id"]]["next_due"]
                    chore["assigned_to"] = schedule[chore["id"]]["assigned_to"]
        except Exception as e:
            self.log(f"Claude scheduling failed, falling back to baseline: {e}", level="WARNING")
            adults = [m for m in self.family if m.get("role") == "adult"]
            fallback_members = adults or self.family
            for i, chore in enumerate(chores):
                chore["next_due"] = self.baseline_next_due(chore).isoformat()
                if not chore.get("assigned_to"):
                    chore["assigned_to"] = fallback_members[i % len(fallback_members)]["name"]

        self.save_chores(chores)
        self.sync_todo_list(chores)

    # ── HA todo sync ──────────────────────────────────────────────────────────

    def sync_todo_list(self, chores):
        today = date.today().isoformat()
        member_entity = {m["name"]: m["todo_entity"] for m in self.family}

        existing_per_entity = {}
        for member in self.family:
            items = self.get_state(member["todo_entity"], attribute="items") or []
            existing_per_entity[member["todo_entity"]] = {
                item["summary"]
                for item in items
                if item.get("status") != "completed"
            }
            for item in items:
                if item.get("status") == "completed":
                    self.call_service(
                        "todo/remove_item",
                        entity_id=member["todo_entity"],
                        item=item["summary"],
                    )

        for chore in chores:
            if chore.get("next_due") != today:
                continue
            assigned = chore.get("assigned_to")
            if not assigned or assigned not in member_entity:
                continue
            entity = member_entity[assigned]
            if chore["name"] not in existing_per_entity.get(entity, set()):
                self.call_service(
                    "todo/add_item",
                    entity_id=entity,
                    item=chore["name"],
                    description=f"{chore['duration']}min · {chore['frequency']}",
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

        member = kwargs.get("member", {})
        for name, status in new_items.items():
            if status == "completed" and old_items.get(name) != "completed":
                self.log(f"{member.get('name', '?')} completed: {name}")
                self.on_chore_completed(name, member)

    def on_chore_completed(self, chore_name, member):
        chores = self.load_chores()
        today = date.today().isoformat()

        for chore in chores:
            if chore["name"] == chore_name:
                skipped = chore.get("skipped_count", 0)
                chore["last_completed"] = today
                chore["skipped_count"] = 0
                self.save_chores(chores)

                peptalk = self.get_peptalk(chore, member.get("name", "You"), skipped)
                notify = member.get("notify_service") or self.global_notify
                self.call_service(
                    notify,
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
            "kids_ok": str(data.get("kids_ok", "false")).lower() == "true",
            "assigned_to": None,
            "last_completed": None,
            "skipped_count": 0,
            "next_due": None,
        })

        self.save_chores(chores)
        self.log(f"Added chore: {data['name']}")
        self.recalculate_and_sync()

    # ── Peptalk ───────────────────────────────────────────────────────────────

    def get_peptalk(self, chore, person_name, skipped_count):
        context = (
            f"skipped {skipped_count} time(s) before finally doing it"
            if skipped_count > 0
            else "done right on schedule"
        )
        prompt = (
            f'{person_name} just finished "{chore["name"]}" '
            f'({chore["duration"]}min, {chore["frequency"]}, {context}). '
            f"Give a short, fun, specific peptalk addressed to {person_name} — 1 sentence max. No emojis."
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
            return f"Nailed it, {person_name} — one less thing to think about."

    def daily_refresh(self, kwargs):
        self.recalculate_and_sync()
