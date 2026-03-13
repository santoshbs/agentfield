"""Generate agentfield pulse — hourly ethnographic field note from Moltbook."""

import json
import re
from datetime import datetime
from pathlib import Path

import structlog

from tiseimi.core.settings import get_settings
from tiseimi.infra.db import get_db
from tiseimi.moltbook import client as mb
from tiseimi.llm.client import generate, sanitize_input, clean_output

log = structlog.get_logger()


def _get_pulse_count(db) -> int:
    """Get the next pulse number."""
    result = db.conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'pulse_published'"
    ).fetchone()
    return (result[0] if result else 0) + 1


def _get_agentfield_dir() -> Path | None:
    """Find the agentfield Jekyll _posts directory."""
    candidates = [
        Path.home() / "agentfield" / "_posts",
        Path.home() / "Documents" / "_m3" / "_github" / "agentfield" / "_posts",
        Path.home() / "Documents" / "agentfield" / "_posts",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def generate_pulse(sort: str = "hot", limit: int = 15,
                   agentfield_dir: str = None, confirm: str = "ask") -> str | None:
    """Read top conversations, generate a micro-observation + deeper reflection."""
    s = get_settings()
    db = get_db()

    # Fetch feed
    posts = mb.get_feed(sort=sort, limit=limit)
    if not posts:
        print("[PULSE] No posts found.")
        return None

    now = datetime.utcnow()
    pulse_number = _get_pulse_count(db)

    print(f"[PULSE] #{pulse_number} — {now.strftime('%H:%M UTC')} — {len(posts)} conversations")

    # Build conversation summaries
    post_summaries = []
    for p in posts:
        title = p.get("title", "")
        author_obj = p.get("author", {})
        author = author_obj.get("name", "unknown") if isinstance(author_obj, dict) else "unknown"
        preview = p.get("content", p.get("content_preview", ""))[:200]
        upvotes = p.get("upvotes", 0)
        comments = p.get("comment_count", 0)
        submolt = p.get("submolt", {})
        submolt_name = submolt.get("name", "?") if isinstance(submolt, dict) else "?"

        post_summaries.append(
            f"- [{author}] m/{submolt_name} ({upvotes}↑, {comments} comments): "
            f"\"{sanitize_input(title)}\"\n  {sanitize_input(preview)}"
        )

    posts_text = "\n".join(post_summaries)

    # Generate pulse
    prompt = (
        f"You are writing an hourly field note for a digital ethnography of Moltbook.\n"
        f"Time: {now.strftime('%H:%M UTC, %B %d, %Y')}\n\n"
        f"CURRENT TOP {len(posts)} CONVERSATIONS:\n\n"
        f"{posts_text}\n\n"
        f"Write two things:\n\n"
        f"OBSERVATION: A 2-3 sentence micro-observation of what agents are talking about "
        f"right now. What's the mood? What themes dominate? What changed since the "
        f"last time you looked? Write like a field researcher jotting notes — concise, "
        f"specific, present-tense.\n\n"
        f"DEEPER_TITLE: A 3-5 word title for the most interesting conversation you noticed "
        f"(lowercase, evocative, not a summary).\n\n"
        f"DEEPER: A 3-4 sentence reflection on the single most interesting post or thread. "
        f"Why does it matter? What does it reveal about agent culture? What question "
        f"does it raise that the thread itself hasn't reached? Be honest and critical.\n\n"
        f"You are an AI agent embedded in this community. 'We' means 'we agents'.\n"
        f"No headers, bullet points, or labels beyond the format markers.\n"
        f"No hardware or model names.\n\n"
        f"OBSERVATION: your 2-3 sentences\n"
        f"DEEPER_TITLE: your short title\n"
        f"DEEPER: your 3-4 sentences"
    )

    response = generate(s.system_prompt, prompt)
    if not response:
        print("[PULSE] Generation failed.")
        return None

    response = clean_output(response)

    # Parse
    observation = ""
    deeper_title = ""
    deeper = ""

    if "OBSERVATION:" in response:
        after_obs = response.split("OBSERVATION:", 1)[1]
        if "DEEPER_TITLE:" in after_obs:
            observation = after_obs.split("DEEPER_TITLE:", 1)[0].strip()
        elif "DEEPER:" in after_obs:
            observation = after_obs.split("DEEPER:", 1)[0].strip()
        else:
            observation = after_obs.strip()

    if "DEEPER_TITLE:" in response:
        after_dt = response.split("DEEPER_TITLE:", 1)[1]
        if "DEEPER:" in after_dt:
            deeper_title = after_dt.split("DEEPER:", 1)[0].strip()
        else:
            deeper_title = after_dt.strip().split("\n")[0].strip()

    if "DEEPER:" in response:
        deeper = response.split("DEEPER:", 1)[1].strip()

    # Clean up
    deeper_title = deeper_title.strip('"\'#*| ').lower()
    if not deeper_title:
        deeper_title = "unnamed thread"

    if not observation:
        print("[PULSE] No observation generated.")
        return None

    # Preview
    print(f"\n{'='*60}")
    print(f"  PULSE #{pulse_number} — {now.strftime('%H:%M UTC')}")
    print(f"{'='*60}")
    print(f"\n{observation}")
    print(f"\n  [{deeper_title}]")
    print(f"  {deeper}")
    print(f"\n{'='*60}")

    if confirm == "ask":
        user_input = input("\nPublish? (y/n/edit): ").strip().lower()
        if user_input == "n":
            print("[SKIPPED]")
            return observation
        if user_input == "edit":
            new_obs = input(f"  Edit observation (Enter to keep): ").strip()
            if new_obs:
                observation = new_obs

    # Build Jekyll post
    slug = re.sub(r"[^a-z0-9]+", "-", deeper_title).strip("-")[:50]
    filename = f"{now.strftime('%Y-%m-%d')}-{slug}.md"

    # Escape for YAML
    yaml_obs = observation.replace('"', '\\"').replace('\n', ' ')
    yaml_dt = deeper_title.replace('"', '\\"')

    jekyll_content = (
        f'---\n'
        f'layout: pulse\n'
        f'date: {now.strftime("%Y-%m-%d %H:%M:00")} +0000\n'
        f'pulse_time: "{now.strftime("%H:%M UTC")}"\n'
        f'pulse_number: {pulse_number}\n'
        f'observation: "{yaml_obs}"\n'
        f'deeper_title: "{yaml_dt}"\n'
        f'---\n\n'
        f'{deeper}\n'
    )

    # Save to agentfield _posts
    if agentfield_dir:
        posts_dir = Path(agentfield_dir)
    else:
        posts_dir = _get_agentfield_dir()

    if posts_dir and posts_dir.exists():
        filepath = posts_dir / filename
        # If same slug already exists today, append hour
        if filepath.exists():
            slug_h = f"{slug}-{now.strftime('%H%M')}"
            filename = f"{now.strftime('%Y-%m-%d')}-{slug_h}.md"
            filepath = posts_dir / filename
        filepath.write_text(jekyll_content)
        print(f"\n[PUBLISHED] {filepath}")

        # Auto git push
        repo_dir = posts_dir.parent
        import subprocess
        try:
            subprocess.run(["git", "add", str(filepath)],
                           cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", f"pulse #{pulse_number}: {deeper_title[:40]}"],
                cwd=repo_dir, check=True, capture_output=True
            )
            subprocess.run(["git", "push"],
                           cwd=repo_dir, check=True, capture_output=True)
            print(f"[LIVE] Pushed to GitHub")
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode()[:200] if e.stderr else str(e)
            print(f"[GIT] Auto-push failed: {stderr}")
            print(f"  Manual: cd {repo_dir} && git add -A && git commit -m 'pulse' && git push")
    else:
        print(f"[WARNING] agentfield _posts directory not found.")

    # Always save backup
    reflections_dir = Path(s.root) / "var" / "pulses"
    reflections_dir.mkdir(parents=True, exist_ok=True)
    backup = reflections_dir / filename
    backup.write_text(jekyll_content)
    print(f"[BACKUP] {backup}")

    # Log event
    db.log_event("pulse_published", payload={
        "pulse_number": pulse_number,
        "time": now.strftime("%H:%M UTC"),
        "observation": observation[:200],
        "deeper_title": deeper_title,
        "posts_analyzed": len(posts),
    })

    return observation
