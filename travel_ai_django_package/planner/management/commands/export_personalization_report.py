# -*- coding: utf-8 -*-
import json
import os
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import UserChatMessage, UserBehaviorEvent

class Command(BaseCommand):
    help = "로그인 사용자 개인화 현황(행동/대화)을 reports 폴더에 JSON/MD로 내보냅니다."

    def add_arguments(self, parser):
        parser.add_argument("--username", type=str, default="", help="특정 username만 내보내기 (미지정 시 전체)")

    def handle(self, *args, **opts):
        username = (opts.get("username") or "").strip()
        from django.contrib.auth import get_user_model
        User = get_user_model()

        qs = User.objects.all()
        if username:
            qs = qs.filter(username=username)

        out_dir = "reports"
        os.makedirs(out_dir, exist_ok=True)

        now = timezone.now()
        for u in qs:
            events = list(UserBehaviorEvent.objects.filter(user=u).order_by("-created_at")[:200].values(
                "created_at","event_type","payload","travel_plan_id","day_number"
            ))
            msgs = list(UserChatMessage.objects.filter(session__user=u, role="assistant").order_by("-created_at")[:50].values(
                "created_at","content","used_llm","sources","actions","session_id"
            ))

            summary = {
                "user": {"id": u.id, "username": u.username, "email": getattr(u, "email", "")},
                "generated_at": now.isoformat(),
                "events_count": len(events),
                "assistant_messages_count": len(msgs),
                "events": events,
                "assistant_messages": msgs,
            }

            json_path = os.path.join(out_dir, f"personalization_{u.username}.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)

            top_events = {}
            for e in events:
                top_events[e["event_type"]] = top_events.get(e["event_type"], 0) + 1
            top_events_sorted = sorted(top_events.items(), key=lambda x: x[1], reverse=True)[:8]

            md_path = os.path.join(out_dir, f"personalization_{u.username}.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(f"# Personalization Report: {u.username}\n\n")
                f.write(f"- Generated at: {now:%Y-%m-%d %H:%M}\n")
                f.write(f"- Events: {len(events)}\n")
                f.write(f"- Assistant messages: {len(msgs)}\n\n")
                f.write("## Top Events\n")
                for k,v in top_events_sorted:
                    f.write(f"- {k}: {v}\n")
                f.write("\n## Recent Assistant Replies (last 5)\n")
                for m in msgs[:5]:
                    snippet = (m.get("content") or "").strip().replace("\n"," ")
                    if len(snippet) > 160:
                        snippet = snippet[:160] + "..."
                    f.write(f"- {m.get('created_at')} | LLM={m.get('used_llm')} | {snippet}\n")

            self.stdout.write(self.style.SUCCESS(f"Exported: {json_path} , {md_path}"))
