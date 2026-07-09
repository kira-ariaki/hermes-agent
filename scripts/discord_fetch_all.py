#!/usr/bin/env python3
"""
Fetch all Discord channels + threads -> save to JSON (per-channel files).
Resumable, rate-limit safe, handles all message types.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# Load .env
env_path = Path.home() / ".hermes" / ".env"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import discord

# Use Kira main bot token from openclaw.json (still valid)
_openclaw_cfg = json.loads((Path.home() / ".openclaw" / "openclaw.json").read_text())
TOKEN = _openclaw_cfg["channels"]["discord"]["accounts"]["main"]["token"]
EXPORT_DIR = Path.home() / ".hermes" / "discord_export"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True
client = discord.Client(intents=intents)


def msg_to_dict(m: discord.Message) -> dict:
    """Serialize a Discord message preserving everything useful."""
    d = {
        "id": str(m.id),
        "ts": m.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "author_id": str(m.author.id),
        "author_name": m.author.display_name,
        "author_username": str(m.author),
        "content": m.content or "",
        "type": str(m.type).split(".")[-1],
    }
    if m.edited_at:
        d["edited_at"] = m.edited_at.replace(tzinfo=timezone.utc).isoformat()
    if m.attachments:
        d["attachments"] = [
            {"url": a.url, "filename": a.filename, "content_type": a.content_type, "size": a.size}
            for a in m.attachments
        ]
    if m.embeds:
        d["embeds"] = [
            {
                "title": e.title,
                "description": e.description,
                "url": e.url,
                "type": e.type,
            }
            for e in m.embeds
        ]
    if m.reference and m.reference.message_id:
        d["reply_to"] = str(m.reference.message_id)
    if m.reactions:
        d["reactions"] = [
            {"emoji": str(r.emoji), "count": r.count} for r in m.reactions
        ]
    if m.mentions:
        d["mentions"] = [{"id": str(u.id), "name": u.display_name} for u in m.mentions]
    if hasattr(m, "thread") and m.thread:
        d["thread_id"] = str(m.thread.id)
    return d


def json_path(ch_name: str, ch_id: int) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ch_name)
    return EXPORT_DIR / f"{safe}_{ch_id}.json"


async def fetch_channel(ch, parent_ch=None):
    """Fetch all messages in a channel/thread, saving incrementally. Resumable via last_id. Retries on transient errors."""
    label = f"#{ch.name}" + (f" (thread in #{parent_ch.name})" if parent_ch else "")
    out_path = json_path(
        (parent_ch.name + "__thread__" + ch.name) if parent_ch else ch.name,
        ch.id,
    )

    # Load existing for resume
    existing = {"channel_id": str(ch.id), "channel_name": ch.name, "parent_channel_id": str(parent_ch.id) if parent_ch else None, "parent_channel_name": parent_ch.name if parent_ch else None, "messages": [], "last_id": None, "finished": False}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except Exception:
            pass
    if existing.get("finished"):
        print(f"[skip-done] {label}: already finished ({len(existing['messages'])} msgs)")
        return

    attempt = 0
    max_attempts = 8
    while attempt < max_attempts:
        resume_after = int(existing["last_id"]) if existing.get("last_id") else None
        print(f"[start] {label} attempt={attempt+1} resume_after={resume_after}", flush=True)
        count = len(existing["messages"])
        batch_count = 0
        try:
            async for m in ch.history(limit=None, oldest_first=True, after=discord.Object(id=resume_after) if resume_after else None):
                existing["messages"].append(msg_to_dict(m))
                existing["last_id"] = str(m.id)
                count += 1
                batch_count += 1
                if batch_count >= 500:
                    out_path.write_text(json.dumps(existing, ensure_ascii=False))
                    print(f"[flush] {label} count={count}", flush=True)
                    batch_count = 0
            # Success: mark finished
            existing["finished"] = True
            existing["fetched_at"] = datetime.now(timezone.utc).isoformat()
            out_path.write_text(json.dumps(existing, ensure_ascii=False))
            print(f"[done]  {label} count={count}", flush=True)
            return
        except discord.Forbidden:
            print(f"[forbidden] {label}", flush=True)
            out_path.write_text(json.dumps(existing, ensure_ascii=False))
            return
        except (discord.HTTPException, discord.DiscordServerError, asyncio.TimeoutError, ConnectionError) as e:
            # Save progress, retry with backoff
            out_path.write_text(json.dumps(existing, ensure_ascii=False))
            attempt += 1
            wait = min(60, 2 ** attempt)
            print(f"[retry {attempt}/{max_attempts}] {label} count={count}: {type(e).__name__}: {e}  sleeping {wait}s", flush=True)
            await asyncio.sleep(wait)
            continue
        except Exception as e:
            print(f"[error] {label} count={count}: {type(e).__name__}: {e}", flush=True)
            out_path.write_text(json.dumps(existing, ensure_ascii=False))
            return
    print(f"[giveup] {label} after {max_attempts} attempts", flush=True)
    out_path.write_text(json.dumps(existing, ensure_ascii=False))


@client.event
async def on_ready():
    try:
        print(f"bot_user={client.user} id={client.user.id}", flush=True)
        for g in client.guilds:
            text_chans = [c for c in g.channels if isinstance(c, discord.TextChannel)]
            # Optional: collect archived threads first (none in this guild, but safe)
            all_threads = []
            for c in text_chans:
                # active threads
                for t in [t for t in g.threads if t.parent_id == c.id]:
                    all_threads.append((t, c))
                # archived (in case)
                try:
                    async for t in c.archived_threads(limit=None):
                        all_threads.append((t, c))
                except Exception:
                    pass
            print(f"channels={len(text_chans)} threads={len(all_threads)}", flush=True)
            # Fetch channels first (biggest bulk)
            for c in text_chans:
                await fetch_channel(c)
            # Then threads
            for t, parent in all_threads:
                await fetch_channel(t, parent_ch=parent)
    finally:
        await client.close()


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
